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
) -> list[dict[str, Any]]:
    """
    Build shot → chunk plan.
    Continuous frames (is_new_shot=False) merge into the current shot.
    """
    settings = get_settings()
    assert_chunk_frames(chunk_frames)
    if overlap_frames < 0 or overlap_frames >= chunk_frames:
        raise ValueError("overlap_frames must be in [0, chunk_frames)")

    steps = steps if steps is not None else settings.default_steps
    cfg = cfg if cfg is not None else settings.default_cfg
    sampler = sampler or settings.default_sampler
    scheduler = scheduler or settings.default_scheduler

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

    # Allocate duration across shots from hints, scaled to target
    hints = [sum(float(f.get("duration_hint_sec") or 4.0) for f in g) for g in groups]
    hint_total = sum(hints) or 1.0
    scale = target_length_sec / hint_total

    shots: list[dict[str, Any]] = []
    for si, group in enumerate(groups):
        shot_dur = max(chunk_frames / fps, hints[si] * scale)
        n_chunks = chunks_for_duration(shot_dur, chunk_frames, fps)
        # Effective new frames per continue ≈ chunk_frames - overlap
        effective = chunk_frames - overlap_frames if n_chunks > 1 else chunk_frames
        # Recalculate with overlap awareness
        needed = int(math.ceil(shot_dur * fps))
        if n_chunks > 1:
            n_chunks = max(1, 1 + int(math.ceil(max(0, needed - chunk_frames) / max(1, effective))))

        base = prompt_base.strip() or ""
        # Prefer this shot's own beats — never dump a multi-scene script into every chunk.
        shot_bits = [
            (f.get("visual_prompt") or f.get("description") or "").strip() for f in group
        ]
        shot_bits = [b for b in shot_bits if b]
        shot_prompt = " ".join(shot_bits) if shot_bits else "cinematic scene"
        # Light world lock from premise/base only when it is not a scene list.
        world = ""
        for candidate in (base,):
            low = candidate.lower()
            if candidate and low.count("scene ") < 2 and "**scene" not in low:
                world = candidate[:320]
                break
        prompt_for_shot = (
            f"{shot_prompt}. Continuity: {world}" if world else shot_prompt
        )
        start_still = (
            group[0].get("keyframe_first_path")
            or group[0].get("still_path")
            or None
        )
        shot_chunks = []
        for ci in range(n_chunks):
            mode = "new_shot" if ci == 0 else "continue"
            delta = group[min(ci, len(group) - 1)].get("visual_prompt") or ""
            if mode == "continue":
                delta = f"continue motion, {delta}" if delta else "continue the same motion and camera"
            handoff = {
                "shot_id": f"shot_{si:02d}",
                "mode": mode,
                "model": "wan2.2",
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
