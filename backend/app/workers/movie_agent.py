from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.db.models import Chunk, RenderJob, SessionLocal, Shot, init_db
from app.services.comfyui import ComfyUIClient, ComfyUIError
from app.services.continuity import compose_prompt
from app.services.ffmpeg import (
    concat_frame_dirs,
    encode_frames_to_mp4,
    extract_frames_from_video,
)
from app.services.frames import (
    discard_overlap,
    qa_join,
    save_tail_overlap,
    write_kept_frames,
)
from app.services.llm import prompt_delta_for_continue
from app.services.workflows import apply_params

logger = logging.getLogger("movie_agent")


def _job_cancelled_or_paused(job_id: int) -> str | None:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            return "missing"
        if job.status == "cancelling":
            job.status = "cancelled"
            db.commit()
            return "cancelled"
        if job.status == "paused":
            return "paused"
        if job.status == "cancelled":
            return "cancelled"
        return None


def _set_job(job_id: int, **fields: Any) -> None:
    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        db.commit()


def _set_chunk(chunk_id: int, **fields: Any) -> None:
    with SessionLocal() as db:
        ch = db.get(Chunk, chunk_id)
        if not ch:
            return
        for k, v in fields.items():
            setattr(ch, k, v)
        db.commit()


async def _run_chunk(
    job: RenderJob,
    shot: Shot,
    chunk: Chunk,
    prev_last_frame: Path | None,
) -> Path | None:
    settings = get_settings()
    handoff = dict(chunk.handoff or {})
    mode = chunk.mode
    overlap = int(handoff.get("overlap_frames") or (job.overlap_frames if mode == "continue" else 0))
    frame_count = int(handoff.get("frame_count") or job.chunk_frames)
    prompt = compose_prompt(handoff)

    workflow_id = job.t2v_workflow if mode == "new_shot" else job.i2v_workflow
    params = {
        "positive_prompt": prompt,
        "negative_prompt": handoff.get("negative_prompt") or job.negative_prompt,
        "seed": int(handoff.get("seed") or job.seed),
        "steps": int(handoff.get("steps") or settings.default_steps),
        "cfg": float(handoff.get("cfg") or settings.default_cfg),
        "sampler_name": handoff.get("sampler") or settings.default_sampler,
        "scheduler": handoff.get("scheduler") or settings.default_scheduler,
        "num_frames": frame_count,
        "width": job.width,
        "height": job.height,
        "fps": job.fps,
        "filename_prefix": f"local_video/job{job.id}/shot{shot.position}/chunk{chunk.chunk_index}",
    }

    comfy = ComfyUIClient()
    uploaded = None
    if mode == "continue":
        last = prev_last_frame or (Path(chunk.last_frame_path) if chunk.last_frame_path else None)
        if last is None or not Path(last).exists():
            # try previous chunk last frame from handoff
            lf = handoff.get("last_frame")
            last = Path(lf) if lf else None
        if last is None or not last.exists():
            raise ComfyUIError("continue mode requires last_frame")
        uploaded = await comfy.upload_image(Path(last))

    graph = apply_params(workflow_id, params, uploaded_image_name=uploaded)
    _set_chunk(chunk.id, status="running")
    prompt_id = await comfy.queue_prompt(graph)
    _set_chunk(chunk.id, comfy_prompt_id=prompt_id)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise ComfyUIError("no outputs from ComfyUI")

    chunk_dir = (
        settings.media_dir
        / "projects"
        / str(job.project_id)
        / "jobs"
        / str(job.id)
        / f"shot_{shot.position:02d}"
        / f"chunk_{chunk.chunk_index:03d}"
    )
    raw_dir = chunk_dir / "raw_frames"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    raw_dir.mkdir(parents=True)

    video_path = None
    image_paths: list[Path] = []
    for out in outputs:
        dest = chunk_dir / out["filename"]
        await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
        if dest.suffix.lower() in {".mp4", ".webm", ".gif", ".mkv", ".mov"}:
            video_path = dest
        elif dest.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            image_paths.append(dest)

    if video_path:
        frames = extract_frames_from_video(video_path, raw_dir)
    elif image_paths:
        # single image treated as 1-frame (still); copy
        frames = write_kept_frames(image_paths, raw_dir)
    else:
        raise ComfyUIError("unsupported output types")

    # Save overlap tail from full raw frames
    save_tail_overlap(frames, overlap if mode == "continue" else min(overlap or job.overlap_frames, len(frames)), chunk_dir / "tail_overlap")

    if mode == "continue":
        kept = discard_overlap(frames, overlap)
    else:
        kept = frames

    kept_dir = chunk_dir / "kept"
    write_kept_frames(kept, kept_dir)
    last_frame = kept[-1] if kept else frames[-1]
    last_path = chunk_dir / "last_frame.png"
    shutil.copy2(last_frame, last_path)

    # Join QA against previous shot/chunk last frame
    ok, note = qa_join(prev_last_frame, kept[0] if kept else None)
    handoff["continuity_notes"] = note
    handoff["last_frame"] = str(last_path)
    handoff["last_frames_dir"] = str(chunk_dir / "tail_overlap")

    if not ok and chunk.retries < 2:
        _set_chunk(
            chunk.id,
            status="failed",
            error=note,
            retries=chunk.retries + 1,
            handoff=handoff,
            frames_dir=str(raw_dir),
            kept_frames_dir=str(kept_dir),
            last_frame_path=str(last_path),
        )
        # tighten delta and retry once more by re-raising special
        raise ComfyUIError(f"join QA failed: {note}")

    _set_chunk(
        chunk.id,
        status="completed",
        error=None if ok else note,
        handoff=handoff,
        frames_dir=str(raw_dir),
        kept_frames_dir=str(kept_dir),
        last_frame_path=str(last_path),
    )
    return last_path


async def run_movie_job(ctx: dict, job_id: int) -> str:
    init_db()
    settings = get_settings()
    flag = _job_cancelled_or_paused(job_id)
    if flag in ("cancelled", "paused", "missing"):
        return flag or "missing"

    _set_job(job_id, status="running", progress={"phase": "starting"}, error=None)

    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            return "missing"
        job_snapshot = {
            "id": job.id,
            "project_id": job.project_id,
            "chunk_frames": job.chunk_frames,
            "overlap_frames": job.overlap_frames,
            "width": job.width,
            "height": job.height,
            "fps": job.fps,
            "t2v_workflow": job.t2v_workflow,
            "i2v_workflow": job.i2v_workflow,
            "negative_prompt": job.negative_prompt,
            "seed": job.seed,
            "format": job.format,
        }
        shot_ids = [s.id for s in job.shots]

    class _JobView:
        pass

    job = _JobView()
    for k, v in job_snapshot.items():
        setattr(job, k, v)

    prev_last: Path | None = None
    kept_dirs: list[Path] = []

    try:
        for shot_id in shot_ids:
            with SessionLocal() as db:
                shot = db.get(Shot, shot_id)
                if not shot:
                    continue
                shot_pos = shot.position
                # detach chunk ids
                chunk_ids = [c.id for c in shot.chunks]
            shot = type("ShotView", (), {"id": shot_id, "position": shot_pos})()
            flag = _job_cancelled_or_paused(job_id)
            if flag:
                return flag
            with SessionLocal() as db:
                s = db.get(Shot, shot.id)
                assert s
                s.status = "running"
                db.commit()

            for chunk_id in chunk_ids:
                flag = _job_cancelled_or_paused(job_id)
                if flag:
                    return flag

                with SessionLocal() as db:
                    ch = db.get(Chunk, chunk_id)
                    if not ch:
                        continue
                    if ch.status == "completed" and ch.kept_frames_dir:
                        kept_dirs.append(Path(ch.kept_frames_dir))
                        if ch.last_frame_path:
                            prev_last = Path(ch.last_frame_path)
                        continue
                    chunk_mode = ch.mode
                    chunk_index = ch.chunk_index
                    handoff = dict(ch.handoff or {})

                # Refresh prompt_delta for continue chunks
                if chunk_mode == "continue":
                    try:
                        delta = await prompt_delta_for_continue(
                            handoff.get("prompt_base") or "",
                            handoff.get("prompt_delta") or "",
                            handoff.get("continuity_notes") or "",
                        )
                        handoff["prompt_delta"] = delta
                        _set_chunk(chunk_id, handoff=handoff)
                    except Exception as e:
                        logger.warning("prompt_delta failed: %s", e)

                _set_job(
                    job_id,
                    progress={
                        "phase": "generating",
                        "shot": shot.position,
                        "chunk": chunk_index,
                    },
                )

                attempts = 0
                while attempts < 3:
                    attempts += 1
                    try:
                        with SessionLocal() as db:
                            chunk = db.get(Chunk, chunk_id)
                            assert chunk
                        last = await _run_chunk(job, shot, chunk, prev_last)
                        with SessionLocal() as db:
                            ch = db.get(Chunk, chunk_id)
                            assert ch
                            if ch.kept_frames_dir:
                                kept_dirs.append(Path(ch.kept_frames_dir))
                            prev_last = Path(ch.last_frame_path) if ch.last_frame_path else last
                        break
                    except Exception as e:
                        logger.exception("chunk failed")
                        _set_chunk(chunk_id, status="failed", error=str(e), retries=attempts)
                        if attempts >= 3:
                            _set_job(job_id, status="failed", error=str(e))
                            return "failed"
                        with SessionLocal() as db:
                            ch = db.get(Chunk, chunk_id)
                            assert ch
                            h = dict(ch.handoff or {})
                            h["overlap_frames"] = min(
                                job.chunk_frames - 1,
                                int(h.get("overlap_frames") or job.overlap_frames) + 4,
                            )
                            ch.handoff = h
                            db.commit()

            with SessionLocal() as db:
                s = db.get(Shot, shot.id)
                assert s
                s.status = "completed"
                db.commit()

        # Stitch
        _set_job(job_id, progress={"phase": "stitching"})
        out_frames = (
            settings.media_dir
            / "projects"
            / str(job.project_id)
            / "jobs"
            / str(job_id)
            / "final_frames"
        )
        concat_frame_dirs(kept_dirs, out_frames)
        movie_path = (
            settings.media_dir
            / "projects"
            / str(job.project_id)
            / "jobs"
            / str(job_id)
            / f"movie.{job.format or 'mp4'}"
        )
        encode_frames_to_mp4(out_frames, movie_path, fps=job.fps)
        _set_job(
            job_id,
            status="completed",
            movie_path=str(movie_path),
            progress={"phase": "done", "frames": len(list(out_frames.glob('*.png')))},
        )
        return "completed"
    except Exception as e:
        logger.exception("job failed")
        _set_job(job_id, status="failed", error=str(e))
        return "failed"


class WorkerSettings:
    functions = [run_movie_job]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 1
    job_timeout = 86400

    @staticmethod
    async def on_startup(ctx: dict) -> None:
        init_db()
