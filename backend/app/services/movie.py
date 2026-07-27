from __future__ import annotations

from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings
from app.db.models import Chunk, Project, RenderJob, SessionLocal, Shot
from app.services.continuity import plan_shots_from_frames
from app.services.workflows import validate_frame_count


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


def job_dict(job: RenderJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "project_id": job.project_id,
        "status": job.status,
        "target_length_sec": job.target_length_sec,
        "format": job.format,
        "aspect": job.aspect,
        "chunk_frames": job.chunk_frames,
        "overlap_frames": job.overlap_frames,
        "width": job.width,
        "height": job.height,
        "fps": job.fps,
        "t2v_workflow": job.t2v_workflow,
        "i2v_workflow": job.i2v_workflow,
        "movie_path": job.movie_path,
        "error": job.error,
        "progress": job.progress or {},
        "shots": [
            {
                "id": s.id,
                "position": s.position,
                "title": s.title,
                "status": s.status,
                "chunks": [
                    {
                        "id": c.id,
                        "chunk_index": c.chunk_index,
                        "mode": c.mode,
                        "status": c.status,
                        "error": c.error,
                        "retries": c.retries,
                        "last_frame_path": c.last_frame_path,
                        "handoff": c.handoff,
                    }
                    for c in s.chunks
                ],
            }
            for s in job.shots
        ],
    }


async def start_movie(
    project_id: int,
    *,
    target_length_sec: float = 30.0,
    format: str = "mp4",
    aspect: str = "16:9",
    chunk_frames: int | None = None,
    overlap_frames: int | None = None,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    t2v_workflow: str = "wan22_t2v",
    i2v_workflow: str = "wan22_i2v",
    prompt_base: str = "",
    negative_prompt: str = "",
    seed: int = 42,
) -> dict[str, Any]:
    settings = get_settings()
    chunk_frames = chunk_frames or settings.chunk_frames
    overlap_frames = overlap_frames if overlap_frames is not None else settings.overlap_frames
    width = width or settings.default_width
    height = height or settings.default_height
    fps = fps or settings.default_fps
    validate_frame_count(chunk_frames)

    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {
                "id": f.id,
                "description": f.description,
                "visual_prompt": f.visual_prompt,
                "duration_hint_sec": f.duration_hint_sec,
                "is_new_shot": f.is_new_shot,
                "still_path": f.still_path,
                "keyframe_first_path": f.keyframe_first_path,
                "keyframe_mid_path": f.keyframe_mid_path,
                "keyframe_last_path": f.keyframe_last_path,
            }
            for f in p.frames
        ]
        # Prefer premise as light world lock; full story scripts make Wan ignore the beat.
        base = prompt_base or p.premise or ""
        neg = negative_prompt or (
            "blurry, watermark, text, static, morphing face, flickering"
        )

        job = RenderJob(
            project_id=project_id,
            status="pending",
            target_length_sec=target_length_sec,
            format=format,
            aspect=aspect,
            chunk_frames=chunk_frames,
            overlap_frames=overlap_frames,
            width=width,
            height=height,
            fps=fps,
            t2v_workflow=t2v_workflow,
            i2v_workflow=i2v_workflow,
            prompt_base=base,
            negative_prompt=neg,
            seed=seed,
            progress={"phase": "queued"},
        )
        db.add(job)
        db.flush()

        plan = plan_shots_from_frames(
            frames,
            target_length_sec=target_length_sec,
            chunk_frames=chunk_frames,
            overlap_frames=overlap_frames,
            fps=fps,
            prompt_base=base,
            negative_prompt=neg,
            seed=seed,
            width=width,
            height=height,
        )
        for shot_spec in plan:
            shot = Shot(
                job_id=job.id,
                position=shot_spec["position"],
                title=shot_spec["title"],
                prompt_base=shot_spec["prompt_base"],
                frame_id=shot_spec.get("frame_id"),
                status="pending",
            )
            db.add(shot)
            db.flush()
            for ch in shot_spec["chunks"]:
                db.add(
                    Chunk(
                        shot_id=shot.id,
                        chunk_index=ch["chunk_index"],
                        mode=ch["mode"],
                        status="pending",
                        handoff=ch["handoff"],
                    )
                )
        db.commit()
        db.refresh(job)
        job_id = job.id
        payload = job_dict(job)

    redis = await create_pool(_redis_settings())
    await redis.enqueue_job("run_movie_job", job_id)
    await redis.aclose()
    return payload


def get_job_status(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        return job_dict(job)


def pause_job(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        if job.status == "running":
            job.status = "paused"
            job.progress = {**(job.progress or {}), "phase": "paused"}
            db.commit()
        return job_dict(job)


async def resume_job(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        if job.status in ("paused", "failed"):
            job.status = "pending"
            job.error = None
            job.progress = {**(job.progress or {}), "phase": "resuming"}
            db.commit()
    redis = await create_pool(_redis_settings())
    await redis.enqueue_job("run_movie_job", job_id)
    await redis.aclose()
    return get_job_status(job_id)


def cancel_job(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        if job.status not in ("completed", "cancelled"):
            job.status = "cancelling"
            job.progress = {**(job.progress or {}), "phase": "cancelling"}
            db.commit()
        return job_dict(job)


def list_assets(project_id: int) -> dict[str, Any]:
    settings = get_settings()
    root = settings.media_dir / "projects" / str(project_id)
    files = []
    if root.exists():
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files.append({"path": str(p), "relative": str(p.relative_to(settings.media_dir))})
    with SessionLocal() as db:
        jobs = (
            db.query(RenderJob)
            .filter(RenderJob.project_id == project_id)
            .order_by(RenderJob.id.desc())
            .all()
        )
        movies = [{"job_id": j.id, "movie_path": j.movie_path, "status": j.status} for j in jobs]
    return {"files": files, "movies": movies}


def get_movie(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            raise KeyError(f"job {job_id} not found")
        return {
            "job_id": job.id,
            "status": job.status,
            "movie_path": job.movie_path,
            "error": job.error,
        }
