"""ARQ-backed batch keyframe generation (resume / pause / cancel)."""

from __future__ import annotations

from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings
from app.db.models import Project, RenderJob, SessionLocal
from app.services.movie import get_job_status, job_dict
from app.services.storyboard import _keyframes_list


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def start_keyframes_job(
    project_id: int,
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frame_ids = [f.id for f in sorted(p.frames, key=lambda x: x.position)]
        if not frame_ids:
            raise ValueError("no storyboard frames")
        slots: list[dict[str, Any]] = []
        for f in sorted(p.frames, key=lambda x: x.position):
            kfs = _keyframes_list(f)
            for i, kf in enumerate(kfs):
                slots.append(
                    {
                        "frame_id": f.id,
                        "index": i,
                        "role": kf.get("role") or "middle",
                        "has_path": bool((kf.get("path") or "").strip()),
                    }
                )
        job = RenderJob(
            project_id=project_id,
            status="pending",
            kind="keyframes",
            target_length_sec=0,
            progress={
                "phase": "queued",
                "kind": "keyframes",
                "skip_existing": bool(skip_existing),
                "frame_ids": frame_ids,
                "total_slots": len(slots),
                "done_slots": 0,
                "current_frame_id": None,
                "current_index": None,
            },
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
        payload = job_dict(job)

    redis = await create_pool(_redis_settings())
    await redis.enqueue_job("run_keyframes_job", job_id)
    await redis.aclose()
    return payload


async def resume_keyframes_job(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        if (getattr(job, "kind", None) or "movie") != "keyframes":
            raise ValueError("job is not a keyframes job")
        if job.status in ("paused", "failed"):
            job.status = "pending"
            job.error = None
            job.progress = {**(job.progress or {}), "phase": "resuming"}
            db.commit()
    redis = await create_pool(_redis_settings())
    await redis.enqueue_job("run_keyframes_job", job_id)
    await redis.aclose()
    return get_job_status(job_id)
