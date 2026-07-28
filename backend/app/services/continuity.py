from __future__ import annotations

import math
from typing import Any

from app.config import get_settings
from app.services.workflows import validate_frame_count


def assert_chunk_frames(n: int) -> int:
    validate_frame_count(n)
    return n


def chunks_for_duration(duration_sec: float, chunk_frames: int, fps: int) -> int:
    """How many chunks needed for approximate duration (before overlap accounting)."""
    assert_chunk_frames(chunk_frames)
    total_frames = max(chunk_frames, int(math.ceil(duration_sec * fps)))
    # With overlap discard, each continue chunk adds (chunk_frames - overlap) new frames
    return max(1, int(math.ceil(total_frames / chunk_frames)))


def snap_frame_count(n: int, *, minimum: int = 5, maximum: int = 81) -> int:
    """Nearest valid Wan frame count (4n+1) within [minimum, maximum]."""
    n = max(minimum, min(maximum, int(n)))
    k = max(1, round((n - 1) / 4))
    out = 4 * k + 1
    if out > maximum:
        out = 4 * ((maximum - 1) // 4) + 1
    if out < minimum:
        out = 5
    validate_frame_count(out)
    return out


def frame_count_for_span(
    t0: float,
    t1: float,
    fps: int,
    *,
    default: int = 33,
    maximum: int = 81,
) -> int:
    dt = max(0.0, float(t1) - float(t0))
    if dt <= 0:
        return snap_frame_count(default, maximum=maximum)
    return snap_frame_count(int(round(dt * fps)) or default, maximum=maximum)


def _normalize_keyframes(fr: dict[str, Any]) -> list[dict[str, Any]]:
    raw = fr.get("keyframes") or []
    if isinstance(raw, list) and raw:
        out: list[dict[str, Any]] = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            path = (item.get("path") or "").strip() or None
            out.append(
                {
                    "index": int(
                        item.get("index") if item.get("index") is not None else i
                    ),
                    "t_sec": float(item.get("t_sec") or 0.0),
                    "role": str(item.get("role") or ("first" if i == 0 else "middle")),
                    "image_prompt": str(
                        item.get("image_prompt") or item.get("prompt") or ""
                    ),
                    "path": path,
                }
            )
        if out:
            out[-1]["role"] = "last" if len(out) > 1 else "first"
            return out
    # Legacy first/mid/last columns
    legacy: list[dict[str, Any]] = []
    first = (fr.get("keyframe_first_path") or "").strip() or None
    mid = (fr.get("keyframe_mid_path") or "").strip() or None
    last = (fr.get("keyframe_last_path") or "").strip() or None
    if first:
        legacy.append(
            {
                "index": 0,
                "t_sec": 0.0,
                "role": "first",
                "image_prompt": str(fr.get("keyframe_first_prompt") or ""),
                "path": first,
            }
        )
    if mid:
        legacy.append(
            {
                "index": len(legacy),
                "t_sec": float(fr.get("duration_hint_sec") or 4.0) / 2.0,
                "role": "middle",
                "image_prompt": str(fr.get("keyframe_mid_prompt") or ""),
                "path": mid,
            }
        )
    if last:
        legacy.append(
            {
                "index": len(legacy),
                "t_sec": float(fr.get("duration_hint_sec") or 4.0),
                "role": "last",
                "image_prompt": str(fr.get("keyframe_last_prompt") or ""),
                "path": last,
            }
        )
    return legacy


def _keyframes_ready(keyframes: list[dict[str, Any]]) -> bool:
    return bool(keyframes) and all((k.get("path") or "").strip() for k in keyframes)


def _transition_prompt(
    *,
    premise: str,
    start_prompt: str,
    end_prompt: str,
) -> str:
    # Keep planner free of storyboard imports (worker still uses the shared helper).
    world = (premise or "").strip()[:280]
    start = (start_prompt or "").strip()[:220]
    end = (end_prompt or "").strip()[:220]
    parts = [
        "Cinematic continuous video, one camera move, smooth motion between two keyframes.",
        "Begin matched to the starting image; progress toward the ending beat.",
    ]
    if world:
        parts.append(f"Film continuity for: {world}.")
    if start:
        parts.append(f"Starting beat: {start}.")
    if end:
        parts.append(f"Move toward: {end}.")
    parts.append(
        "Keep the same cast, wardrobe, and location; do not jump-cut or show a collage."
    )
    return " ".join(parts)


def _shot_prompt(group: list[dict[str, Any]], prompt_base: str) -> str:
    base = prompt_base.strip() or ""
    shot_bits = [
        (f.get("visual_prompt") or f.get("description") or "").strip() for f in group
    ]
    shot_bits = [b for b in shot_bits if b]
    shot_prompt = " ".join(shot_bits) if shot_bits else "cinematic scene"
    world = ""
    low = base.lower()
    if base and low.count("scene ") < 2 and "**scene" not in low:
        world = base[:320]
    return f"{shot_prompt}. Continuity: {world}" if world else shot_prompt


def _flf_handoff(
    *,
    shot_id: str,
    chunk_index: int,
    frame_count: int,
    overlap_frames: int,
    prompt_base: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    fps: int,
    steps: int,
    cfg: float,
    sampler: str,
    scheduler: str,
    start_image_path: str,
    end_image_path: str,
    frame_id: Any,
    video_backend: str = "wan",
) -> dict[str, Any]:
    return {
        "shot_id": shot_id,
        "mode": "flf2v",
        "model": "wan2.2" if video_backend == "wan" else "ltx",
        "video_backend": video_backend,
        "chunk_index": chunk_index,
        "frame_count": frame_count,
        "overlap_frames": overlap_frames,
        "prompt_base": prompt_base,
        "prompt_delta": "",
        "negative_prompt": negative_prompt,
        "seed_policy": "new_shot",
        "seed": seed,
        "size": [width, height],
        "fps": fps,
        "steps": steps,
        "cfg": cfg,
        "sampler": sampler,
        "scheduler": scheduler,
        "continuity_notes": "",
        "start_image_path": start_image_path,
        "end_image_path": end_image_path,
        "frame_id": frame_id,
    }


def _plan_flf_chunks_for_group(
    group: list[dict[str, Any]],
    *,
    si: int,
    prompt_for_shot: str,
    prompt_base: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    fps: int,
    steps: int,
    cfg: float,
    sampler: str,
    scheduler: str,
    max_frames: int,
    video_backend: str = "wan",
) -> list[dict[str, Any]] | None:
    """
    One FLF2V chunk per consecutive keyframe pair (and between continuous beats).
    Returns None when the shot has no usable keyframe series.
    """
    series_by_frame: list[list[dict[str, Any]]] = []
    any_ready = False
    for fr in group:
        kfs = _normalize_keyframes(fr)
        series_by_frame.append(kfs)
        if len(kfs) >= 2 and _keyframes_ready(kfs):
            any_ready = True
    if not any_ready:
        return None

    shot_id = f"shot_{si:02d}"
    chunks: list[dict[str, Any]] = []
    ci = 0

    def add_pair(
        start_kf: dict[str, Any],
        end_kf: dict[str, Any],
        *,
        frame_id: Any,
        beat_prompt: str,
    ) -> None:
        nonlocal ci
        start_path = (start_kf.get("path") or "").strip()
        end_path = (end_kf.get("path") or "").strip()
        if not start_path or not end_path:
            return
        fc = frame_count_for_span(
            float(start_kf.get("t_sec") or 0.0),
            float(end_kf.get("t_sec") or 0.0),
            fps,
            default=33,
            maximum=max_frames,
        )
        prompt = _transition_prompt(
            premise=prompt_base or prompt_for_shot,
            start_prompt=str(start_kf.get("image_prompt") or beat_prompt),
            end_prompt=str(end_kf.get("image_prompt") or beat_prompt),
        )
        # Drop the shared boundary frame when stitching to the previous FLF segment.
        overlap = 1 if ci > 0 else 0
        handoff = _flf_handoff(
            shot_id=shot_id,
            chunk_index=ci,
            frame_count=fc,
            overlap_frames=overlap,
            prompt_base=prompt,
            negative_prompt=negative_prompt,
            seed=seed + ci,
            width=width,
            height=height,
            fps=fps,
            steps=steps,
            cfg=cfg,
            sampler=sampler,
            scheduler=scheduler,
            start_image_path=start_path,
            end_image_path=end_path,
            frame_id=frame_id,
            video_backend=video_backend,
        )
        chunks.append({"chunk_index": ci, "mode": "flf2v", "handoff": handoff})
        ci += 1

    for fi, fr in enumerate(group):
        kfs = series_by_frame[fi]
        beat = (fr.get("visual_prompt") or fr.get("description") or "").strip()
        if len(kfs) >= 2 and _keyframes_ready(kfs):
            for i in range(len(kfs) - 1):
                add_pair(kfs[i], kfs[i + 1], frame_id=fr.get("id"), beat_prompt=beat)
        # Bridge into the next continuous beat in this shot.
        if fi + 1 < len(group):
            nxt = group[fi + 1]
            nxt_kfs = series_by_frame[fi + 1]
            cur_end = kfs[-1] if kfs and (kfs[-1].get("path") or "").strip() else None
            nxt_start = (
                nxt_kfs[0]
                if nxt_kfs and (nxt_kfs[0].get("path") or "").strip()
                else None
            )
            if cur_end and nxt_start:
                # Shared boundary keyframe (continuous beat): do not insert a
                # zero-motion FLF bridge — next beat's internal pairs carry motion.
                if (cur_end.get("path") or "").strip() == (
                    nxt_start.get("path") or ""
                ).strip():
                    continue
                # Treat inter-beat bridges as ~2s when timestamps are independent.
                bridge_start = {
                    **cur_end,
                    "t_sec": 0.0,
                    "image_prompt": cur_end.get("image_prompt")
                    or beat
                    or prompt_for_shot,
                }
                bridge_end = {
                    **nxt_start,
                    "t_sec": 2.0,
                    "image_prompt": nxt_start.get("image_prompt")
                    or (nxt.get("visual_prompt") or nxt.get("description") or ""),
                }
                add_pair(
                    bridge_start,
                    bridge_end,
                    frame_id=fr.get("id"),
                    beat_prompt=beat,
                )

    return chunks or None


def plan_shots_from_frames(
    frames: list[dict[str, Any]],
    *,
    target_length_sec: float,
    chunk_frames: int,
    overlap_frames: int,
    fps: int,
    prompt_base: str,
    negative_prompt: str,
    seed: int,
    width: int,
    height: int,
    steps: int | None = None,
    cfg: float | None = None,
    sampler: str | None = None,
    scheduler: str | None = None,
    video_backend: str = "wan",
    shot_backends: dict[Any, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Build shot → chunk plan.

    When a shot has a ready keyframe series, emit FLF2V segments between consecutive
    keyframes (and between continuous beats). Otherwise fall back to duration-based
    T2V / rolling last-frame I2V chunks.

    shot_backends maps frame_id or group position → backend id for per-shot overrides.
    """
    from app.services.video_backends import normalize_backend_id

    settings = get_settings()
    assert_chunk_frames(chunk_frames)
    if overlap_frames < 0 or overlap_frames >= chunk_frames:
        raise ValueError("overlap_frames must be in [0, chunk_frames)")

    job_backend = normalize_backend_id(video_backend)
    shot_backends = shot_backends or {}

    steps = steps if steps is not None else settings.default_steps
    cfg = cfg if cfg is not None else settings.default_cfg
    sampler = sampler or settings.default_sampler
    scheduler = scheduler or settings.default_scheduler
    max_frames = max(chunk_frames, 81)

    # Group storyboard frames into shots
    groups: list[list[dict[str, Any]]] = []
    for fr in frames:
        if not groups or fr.get("is_new_shot", True):
            groups.append([fr])
        else:
            groups[-1].append(fr)

    if not groups:
        groups = [
            [
                {
                    "description": prompt_base or "scene",
                    "visual_prompt": prompt_base or "cinematic scene",
                    "duration_hint_sec": target_length_sec,
                    "is_new_shot": True,
                }
            ]
        ]

    def _backend_for_group(si: int, group: list[dict[str, Any]]) -> str:
        fid = group[0].get("id")
        for key in (fid, si, str(fid) if fid is not None else None, str(si)):
            if key is None:
                continue
            if key in shot_backends:
                return normalize_backend_id(shot_backends[key])
            if str(key) in shot_backends:
                return normalize_backend_id(shot_backends[str(key)])
        return job_backend

    # Allocate duration across shots from hints, scaled to target (fallback path only)
    hints = [sum(float(f.get("duration_hint_sec") or 4.0) for f in g) for g in groups]
    hint_total = sum(hints) or 1.0
    scale = target_length_sec / hint_total

    shots: list[dict[str, Any]] = []
    for si, group in enumerate(groups):
        prompt_for_shot = _shot_prompt(group, prompt_base)
        shot_backend = _backend_for_group(si, group)
        flf_chunks = _plan_flf_chunks_for_group(
            group,
            si=si,
            prompt_for_shot=prompt_for_shot,
            prompt_base=prompt_base,
            negative_prompt=negative_prompt,
            seed=seed,
            width=width,
            height=height,
            fps=fps,
            steps=steps,
            cfg=cfg,
            sampler=sampler,
            scheduler=scheduler,
            max_frames=max_frames,
            video_backend=shot_backend,
        )
        if flf_chunks is not None:
            shots.append(
                {
                    "position": si,
                    "title": f"Shot {si + 1}",
                    "prompt_base": prompt_for_shot,
                    "frame_id": group[0].get("id"),
                    "video_backend": shot_backend,
                    "chunks": flf_chunks,
                }
            )
            continue

        shot_dur = max(chunk_frames / fps, hints[si] * scale)
        n_chunks = chunks_for_duration(shot_dur, chunk_frames, fps)
        # Effective new frames per continue ≈ chunk_frames - overlap
        effective = chunk_frames - overlap_frames if n_chunks > 1 else chunk_frames
        # Recalculate with overlap awareness
        needed = int(math.ceil(shot_dur * fps))
        if n_chunks > 1:
            n_chunks = max(
                1,
                1 + int(math.ceil(max(0, needed - chunk_frames) / max(1, effective))),
            )

        start_still = (
            group[0].get("keyframe_first_path") or group[0].get("still_path") or None
        )
        shot_chunks = []
        for ci in range(n_chunks):
            mode = "new_shot" if ci == 0 else "continue"
            delta = group[min(ci, len(group) - 1)].get("visual_prompt") or ""
            if mode == "continue":
                delta = (
                    f"continue motion, {delta}"
                    if delta
                    else "continue the same motion and camera"
                )
            handoff = {
                "shot_id": f"shot_{si:02d}",
                "mode": mode,
                "model": "wan2.2" if shot_backend == "wan" else "ltx",
                "video_backend": shot_backend,
                "chunk_index": ci,
                "frame_count": chunk_frames,
                "overlap_frames": overlap_frames if mode == "continue" else 0,
                "prompt_base": prompt_for_shot,
                "prompt_delta": delta if mode == "continue" else "",
                "negative_prompt": negative_prompt,
                "seed_policy": "continue" if mode == "continue" else "new_shot",
                "seed": seed,
                "size": [width, height],
                "fps": fps,
                "steps": steps,
                "cfg": cfg,
                "sampler": sampler,
                "scheduler": scheduler,
                "continuity_notes": "",
                # When set, chunk 0 uses I2V from this storyboard still instead of T2V.
                "start_image_path": start_still if ci == 0 and start_still else None,
            }
            shot_chunks.append({"chunk_index": ci, "mode": mode, "handoff": handoff})
        shots.append(
            {
                "position": si,
                "title": f"Shot {si + 1}",
                "prompt_base": prompt_for_shot,
                "frame_id": group[0].get("id"),
                "video_backend": shot_backend,
                "chunks": shot_chunks,
            }
        )
    return shots


def compose_prompt(handoff: dict[str, Any]) -> str:
    base = (handoff.get("prompt_base") or "").strip()
    delta = (handoff.get("prompt_delta") or "").strip()
    if delta:
        return f"{base}. {delta}"
    return base
