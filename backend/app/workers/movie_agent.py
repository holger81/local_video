from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.db.models import Chunk, RenderJob, SessionLocal, Shot, init_db
from app.services.comfyui import ComfyUIError
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
    overlap = int(
        handoff.get("overlap_frames")
        or (job.overlap_frames if mode == "continue" else 0)
    )
    frame_count = int(handoff.get("frame_count") or job.chunk_frames)
    prompt = compose_prompt(handoff)

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

    from app.services.storyboard import _resolve_media_file
    from app.services.video_backends import get_video_backend, resolve_video_backend

    backend_id = resolve_video_backend(
        handoff=handoff,
        shot_backend=getattr(shot, "video_backend", None),
        job_backend=getattr(job, "video_backend", None),
    )
    backend = get_video_backend(backend_id)
    frame_count = backend.validate_num_frames(frame_count)
    handoff["video_backend"] = backend_id
    label = f"movie_s{shot.position}_c{chunk.chunk_index}"
    filename_prefix = (
        f"local_video/job{job.id}/shot{shot.position}/chunk{chunk.chunk_index}"
    )
    neg = handoff.get("negative_prompt") or job.negative_prompt
    seed = int(handoff.get("seed") or job.seed)
    frame_id = int(handoff.get("frame_id") or 0)
    render_kw = dict(
        project_id=job.project_id,
        frame_id=frame_id,
        prompt=prompt,
        label=label,
        num_frames=frame_count,
        seed=seed,
        width=job.width,
        height=job.height,
        fps=job.fps,
        dest_dir=chunk_dir,
        filename_prefix=filename_prefix,
        negative_prompt=neg,
        steps=int(handoff.get("steps") or settings.default_steps),
        cfg=float(handoff.get("cfg") or settings.default_cfg),
        sampler_name=handoff.get("sampler") or settings.default_sampler,
        scheduler=handoff.get("scheduler") or settings.default_scheduler,
    )

    # LTX-2.3 Skill Destiny timeline (up to 4 keyframe guides + dialog/SFX).
    if mode == "timeline" or handoff.get("segment_paths"):
        seg_refs = handoff.get("segment_paths") or []
        if len(seg_refs) < 2:
            raise ComfyUIError("timeline mode requires segment_paths (≥2)")
        render_tl = getattr(backend, "render_timeline", None)
        if render_tl is None:
            raise ComfyUIError(
                f"backend {backend_id!r} does not support timeline rendering"
            )
        seg_paths = [_resolve_media_file(str(p)) for p in seg_refs]
        size = handoff.get("size") or [job.width, job.height]
        _set_chunk(chunk.id, status="running")
        video_path = await render_tl(
            project_id=job.project_id,
            frame_id=frame_id,
            segment_paths=seg_paths,
            local_prompts=str(handoff.get("local_prompts") or prompt),
            segment_lengths=str(handoff.get("segment_lengths") or ""),
            num_frames=frame_count,
            frames_seg=list(handoff.get("frames_seg") or []),
            idx_seg2=int(handoff.get("idx_seg2") or 0),
            idx_seg3=int(handoff.get("idx_seg3") or 0),
            idx_seg4=int(handoff.get("idx_seg4") or 0),
            label=label,
            seed=seed,
            width=int(size[0]) if size else job.width,
            height=int(size[1]) if size else job.height,
            latent_width=handoff.get("latent_width"),
            latent_height=handoff.get("latent_height"),
            fps=job.fps,
            global_prompt=str(handoff.get("global_prompt") or ""),
            timeline_data=str(handoff.get("timeline_data") or ""),
            negative_prompt=neg,
            dest_dir=chunk_dir,
            filename_prefix=filename_prefix,
        )
        frames = extract_frames_from_video(video_path, raw_dir)
    # Keyframe-locked FLF2V: start + end images, no rolling I2V freewheel.
    elif mode == "flf2v" or handoff.get("end_image_path"):
        start_ref = handoff.get("start_image_path")
        end_ref = handoff.get("end_image_path")
        if not start_ref or not end_ref:
            raise ComfyUIError(
                "flf2v mode requires start_image_path and end_image_path"
            )
        start_path = _resolve_media_file(str(start_ref))
        end_path = _resolve_media_file(str(end_ref))
        _set_chunk(chunk.id, status="running")
        video_path = await backend.render_flf2v(
            start_image=start_path,
            end_image=end_path,
            project_id=job.project_id,
            frame_id=frame_id,
            prompt=prompt,
            label=label,
            num_frames=frame_count,
            seed=seed,
            width=job.width,
            height=job.height,
            fps=job.fps,
            dest_dir=chunk_dir,
            filename_prefix=filename_prefix,
            negative_prompt=neg,
        )
        frames = extract_frames_from_video(video_path, raw_dir)
    else:
        start_still = handoff.get("start_image_path")
        use_i2v = mode == "continue" or (mode == "new_shot" and start_still)
        _set_chunk(chunk.id, status="running")
        if use_i2v:
            if mode == "continue":
                last = prev_last_frame or (
                    Path(chunk.last_frame_path) if chunk.last_frame_path else None
                )
                if last is None or not Path(last).exists():
                    lf = handoff.get("last_frame")
                    last = Path(lf) if lf else None
                if last is None or not last.exists():
                    raise ComfyUIError("continue mode requires last_frame")
                start_path = Path(last)
            else:
                start_path = Path(start_still)
                if not start_path.exists():
                    media_root = settings.media_dir.resolve()
                    raw = str(start_still)
                    for marker in ("/media/", "media/"):
                        idx = raw.find(marker)
                        if idx >= 0:
                            start_path = media_root / raw[idx + len(marker) :]
                            break
                if not start_path.exists():
                    raise ComfyUIError(f"storyboard still not found: {start_still}")
            video_path = await backend.render_i2v(start_image=start_path, **render_kw)
        else:
            video_path = await backend.render_t2v(**render_kw)
        frames = extract_frames_from_video(video_path, raw_dir)

    # Save overlap tail from full raw frames (new_shot/flf2v: keep a small tail for QA only)
    if mode == "continue":
        tail_n = overlap
    elif mode in ("flf2v", "timeline"):
        tail_n = min(max(overlap, 0), len(frames))
    else:
        # Do not treat falsy 0 as "use job overlap" via `or` — new_shot overlap in handoff is 0.
        tail_n = min(job.overlap_frames, len(frames))
        tail_n = min(max(tail_n, 0), len(frames))
    save_tail_overlap(frames, tail_n, chunk_dir / "tail_overlap")

    if mode in ("continue", "flf2v") and overlap > 0:
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

    if not ok and chunk.retries < 2 and mode != "flf2v":
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
            "video_backend": getattr(job, "video_backend", None) or "wan",
            "t2v_workflow": job.t2v_workflow,
            "i2v_workflow": job.i2v_workflow,
            "flf2v_workflow": getattr(job, "flf2v_workflow", None),
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
                shot_backend = getattr(shot, "video_backend", None)
                # detach chunk ids
                chunk_ids = [c.id for c in shot.chunks]
            shot = type(
                "ShotView",
                (),
                {
                    "id": shot_id,
                    "position": shot_pos,
                    "video_backend": shot_backend,
                },
            )()
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

                # Refresh prompt_delta for continue chunks (not FLF2V keyframe locks)
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
                            prev_last = (
                                Path(ch.last_frame_path) if ch.last_frame_path else last
                            )
                        break
                    except Exception as e:
                        logger.exception("chunk failed")
                        _set_chunk(
                            chunk_id, status="failed", error=str(e), retries=attempts
                        )
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
            progress={"phase": "done", "frames": len(list(out_frames.glob("*.png")))},
        )
        return "completed"
    except Exception as e:
        logger.exception("job failed")
        _set_job(job_id, status="failed", error=str(e))
        return "failed"


async def run_keyframes_job(ctx: dict, job_id: int) -> str:
    """Generate keyframe slots for a project with pause/cancel + skip_existing resume."""
    from app.services.storyboard import (
        _keyframes_list,
        generate_frame_keyframes,
        rebuild_frame_keyframe_prompts,
    )

    with SessionLocal() as db:
        job = db.get(RenderJob, job_id)
        if not job:
            return "missing"
        if (getattr(job, "kind", None) or "movie") != "keyframes":
            _set_job(job_id, status="failed", error="not a keyframes job")
            return "failed"
        project_id = job.project_id
        progress = dict(job.progress or {})
        skip_existing = bool(progress.get("skip_existing", True))
        frame_ids = list(progress.get("frame_ids") or [])
        job.status = "running"
        job.progress = {**progress, "phase": "running"}
        db.commit()

    if not frame_ids:
        with SessionLocal() as db:
            from app.db.models import Project

            p = db.get(Project, project_id)
            if not p:
                _set_job(job_id, status="failed", error="project missing")
                return "failed"
            frame_ids = [f.id for f in sorted(p.frames, key=lambda x: x.position)]

    done_slots = int((progress.get("done_slots") or 0))
    errors: list[dict[str, Any]] = list(progress.get("errors") or [])

    try:
        for fid in frame_ids:
            state = _job_cancelled_or_paused(job_id)
            if state:
                return state
            # Ensure prompts exist; preserve paths by index.
            with SessionLocal() as db:
                from app.db.models import StoryboardFrame

                fr = db.get(StoryboardFrame, fid)
                if not fr:
                    continue
                kfs = _keyframes_list(fr)
                needs_prompts = not kfs or any(
                    not (k.get("image_prompt") or "").strip() for k in kfs
                )
            if needs_prompts:
                try:
                    await rebuild_frame_keyframe_prompts(project_id, fid)
                except Exception as e:
                    errors.append({"frame_id": fid, "error": f"rebuild prompts: {e}"})
                    _set_job(
                        job_id,
                        progress={
                            "phase": "running",
                            "kind": "keyframes",
                            "skip_existing": skip_existing,
                            "frame_ids": frame_ids,
                            "current_frame_id": fid,
                            "done_slots": done_slots,
                            "errors": errors[-20:],
                        },
                    )
                    continue

            _set_job(
                job_id,
                progress={
                    "phase": "running",
                    "kind": "keyframes",
                    "skip_existing": skip_existing,
                    "frame_ids": frame_ids,
                    "current_frame_id": fid,
                    "done_slots": done_slots,
                    "errors": errors[-20:],
                },
            )
            try:
                await generate_frame_keyframes(
                    project_id, fid, skip_existing=skip_existing
                )
                with SessionLocal() as db:
                    from app.db.models import StoryboardFrame

                    done_slots = 0
                    for frame_id in frame_ids:
                        row = db.get(StoryboardFrame, frame_id)
                        if not row:
                            continue
                        done_slots += sum(
                            1
                            for k in _keyframes_list(row)
                            if (k.get("path") or "").strip()
                        )
            except Exception as e:
                logger.exception("keyframes frame %s failed", fid)
                errors.append({"frame_id": fid, "error": str(e)})

            state = _job_cancelled_or_paused(job_id)
            if state:
                _set_job(
                    job_id,
                    progress={
                        "phase": state,
                        "kind": "keyframes",
                        "skip_existing": skip_existing,
                        "frame_ids": frame_ids,
                        "current_frame_id": fid,
                        "done_slots": done_slots,
                        "errors": errors[-20:],
                    },
                )
                return state

        _set_job(
            job_id,
            status="completed",
            progress={
                "phase": "done",
                "kind": "keyframes",
                "skip_existing": skip_existing,
                "frame_ids": frame_ids,
                "done_slots": done_slots,
                "errors": errors[-20:],
            },
            error="; ".join(e["error"] for e in errors[:3]) if errors else None,
        )
        return "completed"
    except Exception as e:
        logger.exception("keyframes job failed")
        _set_job(job_id, status="failed", error=str(e))
        return "failed"


class WorkerSettings:
    functions = [run_movie_job, run_keyframes_job]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 1
    job_timeout = 86400

    @staticmethod
    async def on_startup(ctx: dict) -> None:
        init_db()
