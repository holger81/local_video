from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Project, SessionLocal, StoryboardFrame
from app.services import llm
from app.services.comfyui import ComfyUIClient
from app.services.ffmpeg import (
    concat_frame_dirs,
    concat_videos,
    encode_frames_to_mp4,
    extract_frames_from_video,
)
from app.services.workflows import apply_params, validate_frame_count


def _frame_dict(f: StoryboardFrame) -> dict[str, Any]:
    return {
        "id": f.id,
        "position": f.position,
        "description": f.description,
        "visual_prompt": f.visual_prompt,
        "still_path": f.still_path,
        "keyframe_first_path": f.keyframe_first_path,
        "keyframe_mid_path": f.keyframe_mid_path,
        "keyframe_last_path": f.keyframe_last_path,
        "keyframe_first_prompt": f.keyframe_first_prompt or "",
        "keyframe_mid_prompt": f.keyframe_mid_prompt or "",
        "keyframe_last_prompt": f.keyframe_last_prompt or "",
        "preview_path": f.preview_path,
        "duration_hint_sec": f.duration_hint_sec,
        "is_new_shot": f.is_new_shot,
    }


_KEYFRAME_PHASES: tuple[tuple[str, str, str], ...] = (
    (
        "first",
        "keyframe_first_path",
        "Opening keyframe of this beat — establishing shot, action just beginning.",
    ),
    (
        "mid",
        "keyframe_mid_path",
        "Midpoint keyframe of this beat — peak action and strongest pose.",
    ),
    (
        "last",
        "keyframe_last_path",
        "Closing keyframe of this beat — action resolving, ready to continue.",
    ),
)

_KEYFRAME_PROMPT_ATTR = {
    "first": "keyframe_first_prompt",
    "mid": "keyframe_mid_prompt",
    "last": "keyframe_last_prompt",
}

_KEYFRAME_PATH_ATTR = {
    "first": "keyframe_first_path",
    "mid": "keyframe_mid_path",
    "last": "keyframe_last_path",
}


def _phase_prompt(
    *,
    phase_label: str,
    beat: str,
    premise: str,
    next_beat: str | None,
) -> str:
    world = _truncate(premise or "", 280)
    beat_bit = _truncate(beat or "", 280)
    parts = [
        "Photorealistic cinematic still photograph, one continuous camera shot, one moment only.",
        phase_label,
    ]
    if world:
        parts.append(f"Film continuity for: {world}.")
    if beat_bit:
        parts.append(f"Beat: {beat_bit}.")
    if next_beat and "Closing" in phase_label:
        parts.append(f"Leans toward the next beat: {_truncate(next_beat, 160)}.")
    parts.append(
        "Same cast, wardrobe, and location look; do not show a multi-panel layout."
    )
    return " ".join(parts)


def _seed_all_keyframe_prompts(project_id: int, *, force: bool = False) -> None:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = sorted(p.frames, key=lambda x: x.position)
        premise = p.premise or ""
        for i, f in enumerate(frames):
            beat = f.visual_prompt or f.description or ""
            next_beat = None
            if i + 1 < len(frames):
                nxt = frames[i + 1]
                next_beat = nxt.visual_prompt or nxt.description or ""
            for phase, _path_attr, label in _KEYFRAME_PHASES:
                attr = _KEYFRAME_PROMPT_ATTR[phase]
                if not force and (getattr(f, attr) or "").strip():
                    continue
                setattr(
                    f,
                    attr,
                    _phase_prompt(
                        phase_label=label,
                        beat=beat,
                        premise=premise,
                        next_beat=next_beat,
                    ),
                )
        db.commit()


def rebuild_frame_keyframe_prompts(project_id: int, frame_id: int) -> dict[str, Any]:
    """Recompute first/mid/last keyframe prompts from beat + premise (+ next beat)."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        beat = f.visual_prompt or f.description or ""
        premise = p.premise or ""
        next_beat = None
        if idx + 1 < len(frames):
            nxt = frames[idx + 1]
            next_beat = nxt.visual_prompt or nxt.description or ""
        for phase, _path_attr, label in _KEYFRAME_PHASES:
            setattr(
                f,
                _KEYFRAME_PROMPT_ATTR[phase],
                _phase_prompt(
                    phase_label=label,
                    beat=beat,
                    premise=premise,
                    next_beat=next_beat,
                ),
            )
        db.commit()
        db.refresh(f)
        return _frame_dict(f)


def _frames_payload(project_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        return [_frame_dict(f) for f in p.frames]


async def propose_storyboard(project_id: int, max_frames: int = 8) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        story = p.story or p.premise
        if not story:
            raise ValueError("project has no story/premise")
        # replace frames
        for f in list(p.frames):
            db.delete(f)
        db.commit()

    proposed = await llm.propose_storyboard(story, max_frames=max_frames)
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        assert p is not None
        p.storyboard_approved = False
        for item in proposed:
            db.add(
                StoryboardFrame(
                    project_id=project_id,
                    position=item["position"],
                    description=item["description"],
                    visual_prompt=item["visual_prompt"],
                    duration_hint_sec=item["duration_hint_sec"],
                    is_new_shot=item["is_new_shot"],
                )
            )
        db.commit()
    _seed_all_keyframe_prompts(project_id, force=True)
    return _frames_payload(project_id)


def update_frame(project_id: int, frame_id: int, **fields: Any) -> dict[str, Any]:
    allowed = {
        "description",
        "visual_prompt",
        "duration_hint_sec",
        "is_new_shot",
        "position",
        "still_path",
        "preview_path",
        "keyframe_first_prompt",
        "keyframe_mid_prompt",
        "keyframe_last_prompt",
    }
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        for k, v in fields.items():
            if k in allowed and v is not None:
                setattr(f, k, v)
        db.commit()
        db.refresh(f)
        return _frame_dict(f)


def delete_frame_media(project_id: int, frame_id: int, kind: str) -> dict[str, Any]:
    """Clear still, preview, or a keyframe path and remove the file if under media_dir."""
    kind_to_attr = {
        "still": "still_path",
        "preview": "preview_path",
        "keyframe_first": "keyframe_first_path",
        "keyframe_mid": "keyframe_mid_path",
        "keyframe_last": "keyframe_last_path",
    }
    if kind not in kind_to_attr:
        raise ValueError(
            "kind must be still, preview, keyframe_first, keyframe_mid, or keyframe_last"
        )
    settings = get_settings()
    path_attr = kind_to_attr[kind]
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        old = getattr(f, path_attr)
        setattr(f, path_attr, None)
        db.commit()
        db.refresh(f)
        payload = _frame_dict(f)
        payload["deleted"] = kind

    if old:
        try:
            p = Path(old)
            media_root = settings.media_dir.resolve()
            if p.is_file() and str(p.resolve()).startswith(str(media_root)):
                p.unlink()
        except OSError:
            pass
    return payload


def approve_storyboard(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        if not p.frames:
            raise ValueError("no storyboard frames")
        p.storyboard_approved = True
        db.commit()
        return {"id": p.id, "storyboard_approved": True}


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


_SCENE_LIST_RE = re.compile(r"(?i)\bscene\s*\d+\b")


def _is_scene_list(text: str) -> bool:
    """True when text enumerates multiple scenes (Flux draws these as grids)."""
    return len(_SCENE_LIST_RE.findall(text or "")) >= 2


def _world_lock(*, premise: str, story: str) -> str:
    """Short cast/setting lock for image models — never a multi-scene script."""
    premise_bit = _truncate(premise or "", 320)
    if premise_bit:
        return premise_bit
    story_bit = (story or "").strip()
    if not story_bit or _is_scene_list(story_bit):
        return ""
    return _truncate(story_bit, 320)


def _frame_wants_on_screen_text(frame_prompt: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(text|title card|end card|credits|neon|screen|sign|caption|subtitle)\b|"
            r"[\"'].+[\"']",
            frame_prompt or "",
        )
    )


def still_negative_prompt(frame_prompt: str = "") -> str:
    base = (
        "blurry, watermark, logo, inconsistent characters, different person each frame, "
        "style change, collage, comic, manga, storyboard, panels, panel layout, grid, "
        "split screen, montage, multiple images, contact sheet, triptych, scrapbook, "
        "comic strip, multi-panel, 2x2, 3x2, tiled images, film strip, border frames"
    )
    if _frame_wants_on_screen_text(frame_prompt):
        return base
    return base + ", text overlay, on-screen text, title card, end card, neon sign text"


def build_visual_prompt(
    *,
    story: str,
    title: str,
    genre: str,
    frame_prompt: str,
    premise: str = "",
    prev_prompt: str | None = None,
    next_prompt: str | None = None,
) -> str:
    """Compose an image prompt for ONE shot with light continuity lock.

    Do not dump multi-scene story scripts or neighbor beats into the prompt —
    Flux/Klein often renders those as a literal storyboard collage. Prefer the
    short premise as world lock; ignore prev/next scene text (kept in signature
    for callers / future soft continuity).
    """
    _ = title, prev_prompt, next_prompt  # title/neighbors unused for image models
    frame_bit = _truncate(frame_prompt or "", 400)
    world = _world_lock(premise=premise, story=story)
    parts: list[str] = [
        "Photorealistic cinematic still photograph, one continuous camera shot, "
        "one moment only, full frame."
    ]
    if genre:
        parts.append(f"{genre} genre.")
    if world:
        parts.append(f"Film continuity for: {world}.")
    parts.append(f"Show only this beat: {frame_bit}.")
    parts.append(
        "Same cast, wardrobe, and location look; do not show other story beats "
        "or a multi-panel layout."
    )
    return " ".join(parts)


async def generate_frame_visual(
    project_id: int,
    frame_id: int,
    *,
    kind: str = "still",
    workflow_id: str | None = None,
    num_frames: int = 33,
) -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frame_prompt = f.visual_prompt or f.description or ""
        prompt = build_visual_prompt(
            story=p.story or "",
            premise=p.premise or "",
            title=p.title or "",
            genre=p.genre or "",
            frame_prompt=frame_prompt,
        )

    if kind == "still":
        workflow_id = workflow_id or "still_hero"
        params = {
            "positive_prompt": prompt,
            "negative_prompt": still_negative_prompt(frame_prompt),
            "seed": frame_id * 17,
            "filename_prefix": f"local_video/p{project_id}_f{frame_id}_still",
        }
    else:
        workflow_id = workflow_id or "wan22_t2v"
        validate_frame_count(num_frames)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": (
                "blurry, watermark, text, static, inconsistent characters, style change, "
                "collage, comic, storyboard, panels, grid, split screen, montage, "
                "multi-panel, contact sheet"
            ),
            "seed": frame_id * 17,
            "num_frames": num_frames,
            "width": settings.default_width,
            "height": settings.default_height,
            "fps": settings.default_fps,
            "cfg": settings.default_cfg,
            "filename_prefix": f"local_video/p{project_id}_f{frame_id}_preview",
        }

    graph = apply_params(workflow_id, params)
    comfy = ComfyUIClient()
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    saved_path = None
    for out in outputs:
        dest = media / out["filename"]
        await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
        saved_path = dest
        # Preview clips: only seed a still from the first frame when none exists yet.
        if out["kind"] in ("gifs", "videos") or dest.suffix.lower() in {".mp4", ".webm", ".gif"}:
            with SessionLocal() as db:
                fr = db.get(StoryboardFrame, frame_id)
                assert fr
                had_still = bool(fr.still_path)
                fr.preview_path = str(dest)
                still_out = fr.still_path
                if not had_still:
                    frames_dir = media / "extracted"
                    frames = extract_frames_from_video(dest, frames_dir)
                    if frames:
                        still = media / "still_from_preview.png"
                        still.write_bytes(frames[0].read_bytes())
                        fr.still_path = str(still)
                        still_out = str(still)
                db.commit()
            return {
                "frame_id": frame_id,
                "kind": kind,
                "preview_path": str(dest),
                "still_path": still_out,
                "prompt_id": prompt_id,
            }

    if saved_path:
        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            if kind == "still":
                fr.still_path = str(saved_path)
            else:
                fr.preview_path = str(saved_path)
            db.commit()
        return {
            "frame_id": frame_id,
            "kind": kind,
            "still_path": str(saved_path) if kind == "still" else None,
            "preview_path": str(saved_path) if kind != "still" else None,
            "prompt_id": prompt_id,
        }
    raise RuntimeError("ComfyUI produced no outputs")


def _resolve_media_file(stored: str) -> Path:
    """Resolve a DB media path (/media/...) to a local file under MEDIA_DIR."""
    settings = get_settings()
    raw = (stored or "").strip()
    if not raw:
        raise FileNotFoundError("empty media path")
    direct = Path(raw)
    if direct.is_file():
        return direct
    rel = raw
    for marker in ("/media/", "media/"):
        idx = raw.find(marker)
        if idx >= 0:
            rel = raw[idx + len(marker) :]
            break
    candidate = (settings.media_dir / rel).resolve()
    media_root = settings.media_dir.resolve()
    if not str(candidate).startswith(str(media_root)):
        raise ValueError("media path escapes MEDIA_DIR")
    if not candidate.is_file():
        raise FileNotFoundError(f"media file not found: {stored}")
    return candidate


def build_edit_prompt(*, instruction: str, frame_prompt: str = "") -> str:
    instr = _truncate((instruction or "").strip(), 500)
    if not instr:
        raise ValueError("edit instruction is required")
    beat = _truncate(frame_prompt or "", 220)
    parts = [
        "Edit this cinematic still photograph.",
        f"Instruction: {instr}.",
        "Preserve composition, camera angle, character identity, wardrobe style, "
        "and setting unless the instruction explicitly changes them.",
        "Output one continuous camera shot only — no collage, panels, or grid.",
    ]
    if beat:
        parts.append(f"Original beat context: {beat}.")
    return " ".join(parts)


async def edit_frame_still(
    project_id: int,
    frame_id: int,
    *,
    instruction: str,
    workflow_id: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Prompt-edit an existing still (Flux ReferenceLatent), replacing the still file."""
    settings = get_settings()
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        if not f.still_path:
            raise ValueError("frame has no still to edit — generate one first")
        still_stored = f.still_path
        frame_prompt = f.visual_prompt or f.description or ""

    source = _resolve_media_file(still_stored)
    prompt = build_edit_prompt(instruction=instruction, frame_prompt=frame_prompt)
    workflow_id = workflow_id or "still_edit"
    comfy = ComfyUIClient()
    uploaded = await comfy.upload_image(source)
    params = {
        "positive_prompt": prompt,
        "negative_prompt": still_negative_prompt(frame_prompt),
        "seed": seed if seed is not None else (frame_id * 17 + 91),
        "filename_prefix": f"local_video/p{project_id}_f{frame_id}_edit",
        "width": 1024,
        "height": 576,
        "steps": 20,
        "cfg": 5.0,
    }
    graph = apply_params(workflow_id, params, uploaded_image_name=uploaded)
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    # Drop previous still file when it is a different path under media_dir.
    try:
        old = _resolve_media_file(still_stored)
        if old.resolve() != dest.resolve() and old.is_file():
            old.unlink()
    except (FileNotFoundError, ValueError, OSError):
        pass

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        fr.still_path = str(dest)
        db.commit()

    return {
        "frame_id": frame_id,
        "kind": "still_edit",
        "still_path": str(dest),
        "instruction": instruction,
        "prompt_id": prompt_id,
    }


async def generate_all_stills(
    project_id: int,
    *,
    workflow_id: str | None = None,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Generate a still for every storyboard frame missing one (sequential)."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {"id": f.id, "still_path": f.still_path}
            for f in sorted(p.frames, key=lambda x: x.position)
        ]
    if not frames:
        raise ValueError("no storyboard frames")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fr in frames:
        if skip_existing and fr.get("still_path"):
            results.append({"frame_id": fr["id"], "skipped": True, "still_path": fr["still_path"]})
            continue
        try:
            out = await generate_frame_visual(
                project_id,
                fr["id"],
                kind="still",
                workflow_id=workflow_id,
            )
            results.append(out)
        except Exception as e:
            errors.append({"frame_id": fr["id"], "error": str(e)})

    return {
        "project_id": project_id,
        "generated": len([r for r in results if not r.get("skipped")]),
        "skipped": len([r for r in results if r.get("skipped")]),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


def build_transition_prompt(
    *,
    premise: str,
    start_prompt: str,
    end_prompt: str,
) -> str:
    """Prompt for a clip that starts on one still and moves toward the next."""
    world = _truncate(premise or "", 280)
    start = _truncate(start_prompt or "", 220)
    end = _truncate(end_prompt or "", 220)
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


async def generate_between_stills(
    project_id: int,
    frame_id: int,
    *,
    workflow_id: str | None = None,
    num_frames: int = 33,
) -> dict[str, Any]:
    """Generate a preview clip from this frame's still toward the next frame's still.

    Uses Wan I2V with the current still as start_image. True dual-keyframe FLF needs a
    FLF2V checkpoint (not on the TI2V 5B host); the next still guides via prompt.
    """
    settings = get_settings()
    validate_frame_count(num_frames)
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next((i for i, fr in enumerate(frames) if fr.id == frame_id), None)
        if idx is None:
            raise KeyError(f"frame {frame_id} not found")
        if idx + 1 >= len(frames):
            raise ValueError("last frame has no next still to transition toward")
        cur = frames[idx]
        nxt = frames[idx + 1]
        # Prefer step keyframes when present: last of this step → first of next.
        start_stored = cur.keyframe_last_path or cur.still_path
        end_ref = nxt.keyframe_first_path or nxt.still_path
        if not start_stored:
            raise ValueError(
                "current frame has no last keyframe or still — create keyframes/stills first"
            )
        if not end_ref:
            raise ValueError(
                "next frame has no first keyframe or still — create keyframes/stills first"
            )
        start_beat = cur.visual_prompt or cur.description or ""
        end_beat = nxt.visual_prompt or nxt.description or ""
        premise = p.premise or ""
        next_frame_id = nxt.id

    start_path = _resolve_media_file(start_stored)
    prompt = build_transition_prompt(
        premise=premise, start_prompt=start_beat, end_prompt=end_beat
    )
    workflow_id = workflow_id or "wan22_i2v"
    comfy = ComfyUIClient()
    uploaded = await comfy.upload_image(start_path)
    params = {
        "positive_prompt": prompt,
        "negative_prompt": (
            "blurry, watermark, text, static, jump cut, morphing face, flickering, "
            "collage, comic, storyboard, panels, grid, split screen, montage"
        ),
        "seed": frame_id * 17 + 3,
        "num_frames": num_frames,
        "width": settings.default_width,
        "height": settings.default_height,
        "fps": settings.default_fps,
        "cfg": settings.default_cfg,
        "filename_prefix": f"local_video/p{project_id}_f{frame_id}_between",
    }
    graph = apply_params(workflow_id, params, uploaded_image_name=uploaded)
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        # Never overwrite the still — only the bridging preview clip.
        fr.preview_path = str(dest)
        db.commit()

    return {
        "frame_id": frame_id,
        "next_frame_id": next_frame_id,
        "kind": "between_stills",
        "preview_path": str(dest),
        "prompt_id": prompt_id,
    }


async def generate_all_between_stills(
    project_id: int,
    *,
    workflow_id: str | None = None,
    skip_existing: bool = True,
    num_frames: int = 33,
) -> dict[str, Any]:
    """Generate between-stills clips for every consecutive still pair."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {
                "id": f.id,
                "still_path": f.still_path,
                "preview_path": f.preview_path,
                "keyframe_last_path": f.keyframe_last_path,
                "keyframe_first_path": f.keyframe_first_path,
            }
            for f in sorted(p.frames, key=lambda x: x.position)
        ]
    if len(frames) < 2:
        raise ValueError("need at least two storyboard frames")

    pairs = []
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        a_ok = a.get("keyframe_last_path") or a.get("still_path")
        b_ok = b.get("keyframe_first_path") or b.get("still_path")
        if a_ok and b_ok:
            pairs.append(a)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    for fr in pairs:
        if skip_existing and fr.get("preview_path"):
            results.append(
                {
                    "frame_id": fr["id"],
                    "skipped": True,
                    "preview_path": fr["preview_path"],
                }
            )
            skipped += 1
            continue
        try:
            out = await generate_between_stills(
                project_id,
                fr["id"],
                workflow_id=workflow_id,
                num_frames=num_frames,
            )
            results.append(out)
        except Exception as e:
            errors.append({"frame_id": fr["id"], "error": str(e)})

    return {
        "project_id": project_id,
        "generated": len([r for r in results if not r.get("skipped")]),
        "skipped": skipped,
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


async def _render_keyframe_image(
    *,
    project_id: int,
    frame_id: int,
    phase: str,
    prompt: str,
    source_still: str | None,
    seed: int,
) -> Path:
    """Generate one keyframe image via edit-from-still when possible, else T2I."""
    settings = get_settings()
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    comfy = ComfyUIClient()
    neg = still_negative_prompt(prompt)

    if source_still:
        src = _resolve_media_file(source_still)
        uploaded = await comfy.upload_image(src)
        edit_prompt = (
            f"Edit this cinematic still. Instruction: {prompt} "
            "Preserve composition, character identity, and setting unless the instruction changes them. "
            "One continuous camera shot only — no collage or panels."
        )
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{phase}",
                "width": 1024,
                "height": 576,
                "steps": 20,
                "cfg": 5.0,
            },
            uploaded_image_name=uploaded,
        )
    else:
        graph = apply_params(
            "still_hero",
            {
                "positive_prompt": prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{phase}",
            },
        )

    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError(f"ComfyUI produced no outputs for keyframe {phase}")
    out = outputs[0]
    dest = media / f"keyframe_{phase}_{out['filename']}"
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


async def generate_frame_keyframes(
    project_id: int,
    frame_id: int,
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Create first / mid / last keyframe stills for one storyboard step."""
    _seed_all_keyframe_prompts(project_id, force=False)
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        source_still = f.still_path
        existing = {
            "first": f.keyframe_first_path,
            "mid": f.keyframe_mid_path,
            "last": f.keyframe_last_path,
        }
        prompts = {
            "first": f.keyframe_first_prompt or "",
            "mid": f.keyframe_mid_prompt or "",
            "last": f.keyframe_last_prompt or "",
        }

    paths: dict[str, str | None] = dict(existing)
    generated: list[str] = []
    skipped: list[str] = []
    for i, (phase, attr, _label) in enumerate(_KEYFRAME_PHASES):
        if skip_existing and existing.get(phase):
            skipped.append(phase)
            continue
        prompt = (prompts.get(phase) or "").strip()
        if not prompt:
            raise ValueError(f"frame {frame_id} has no {phase} keyframe prompt")
        dest = await _render_keyframe_image(
            project_id=project_id,
            frame_id=frame_id,
            phase=phase,
            prompt=prompt,
            source_still=source_still,
            seed=frame_id * 31 + i * 97,
        )
        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            setattr(fr, attr, str(dest))
            db.commit()
        paths[phase] = str(dest)
        generated.append(phase)

    return {
        "frame_id": frame_id,
        "kind": "keyframes",
        "generated": generated,
        "skipped": skipped,
        "keyframe_first_path": paths.get("first"),
        "keyframe_mid_path": paths.get("mid"),
        "keyframe_last_path": paths.get("last"),
    }


async def generate_one_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Render a single keyframe phase from its stored prompt."""
    if phase not in _KEYFRAME_PATH_ATTR:
        raise ValueError("phase must be first, mid, or last")
    _seed_all_keyframe_prompts(project_id, force=False)
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        prompt = (getattr(f, _KEYFRAME_PROMPT_ATTR[phase]) or "").strip()
        source_still = f.still_path
        old_path = getattr(f, _KEYFRAME_PATH_ATTR[phase])
    if not prompt:
        raise ValueError(f"frame {frame_id} has no {phase} keyframe prompt")
    phase_index = next(i for i, (p, *_rest) in enumerate(_KEYFRAME_PHASES) if p == phase)
    dest = await _render_keyframe_image(
        project_id=project_id,
        frame_id=frame_id,
        phase=phase,
        prompt=prompt,
        source_still=source_still,
        seed=seed if seed is not None else (frame_id * 31 + phase_index * 97),
    )
    if old_path:
        try:
            old = _resolve_media_file(old_path)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass
    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        setattr(fr, _KEYFRAME_PATH_ATTR[phase], str(dest))
        db.commit()
        db.refresh(fr)
        return _frame_dict(fr)


async def edit_frame_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    *,
    instruction: str,
    seed: int | None = None,
) -> dict[str, Any]:
    """Prompt-edit an existing keyframe (or still as fallback) into that phase slot."""
    if phase not in _KEYFRAME_PATH_ATTR:
        raise ValueError("phase must be first, mid, or last")
    settings = get_settings()
    path_attr = _KEYFRAME_PATH_ATTR[phase]
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        keyframe_stored = getattr(f, path_attr)
        source_stored = keyframe_stored or f.still_path
        if not source_stored:
            raise ValueError(
                f"frame has no {phase} keyframe or still to edit — generate one first"
            )
        frame_prompt = (
            getattr(f, _KEYFRAME_PROMPT_ATTR[phase])
            or f.visual_prompt
            or f.description
            or ""
        )

    source = _resolve_media_file(source_stored)
    prompt = build_edit_prompt(instruction=instruction, frame_prompt=frame_prompt)
    comfy = ComfyUIClient()
    uploaded = await comfy.upload_image(source)
    params = {
        "positive_prompt": prompt,
        "negative_prompt": still_negative_prompt(frame_prompt),
        "seed": seed if seed is not None else (frame_id * 31 + 53),
        "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{phase}_edit",
        "width": 1024,
        "height": 576,
        "steps": 20,
        "cfg": 5.0,
    }
    graph = apply_params("still_edit", params, uploaded_image_name=uploaded)
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / f"keyframe_{phase}_{out['filename']}"
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    if keyframe_stored:
        try:
            old = _resolve_media_file(keyframe_stored)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        setattr(fr, path_attr, str(dest))
        db.commit()
        db.refresh(fr)
        return _frame_dict(fr)


async def generate_all_keyframes(
    project_id: int,
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Generate first/mid/last keyframes for every storyboard frame."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frame_ids = [f.id for f in sorted(p.frames, key=lambda x: x.position)]
    if not frame_ids:
        raise ValueError("no storyboard frames")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fid in frame_ids:
        try:
            results.append(
                await generate_frame_keyframes(
                    project_id, fid, skip_existing=skip_existing
                )
            )
        except Exception as e:
            errors.append({"frame_id": fid, "error": str(e)})

    return {
        "project_id": project_id,
        "frames": len(frame_ids),
        "completed": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


async def _i2v_clip_from_image(
    *,
    project_id: int,
    frame_id: int,
    start_image: Path,
    prompt: str,
    label: str,
    num_frames: int,
    seed: int,
    workflow_id: str = "wan22_i2v",
) -> Path:
    settings = get_settings()
    validate_frame_count(num_frames)
    comfy = ComfyUIClient()
    uploaded = await comfy.upload_image(start_image)
    params = {
        "positive_prompt": prompt,
        "negative_prompt": (
            "blurry, watermark, text, static, jump cut, morphing face, flickering, "
            "collage, comic, storyboard, panels, grid, split screen, montage"
        ),
        "seed": seed,
        "num_frames": num_frames,
        "width": settings.default_width,
        "height": settings.default_height,
        "fps": settings.default_fps,
        "cfg": settings.default_cfg,
        "filename_prefix": f"local_video/p{project_id}_f{frame_id}_{label}",
    }
    graph = apply_params(workflow_id, params, uploaded_image_name=uploaded)
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


async def generate_step_clips(
    project_id: int,
    frame_id: int,
    *,
    num_frames: int = 33,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """I2V first→mid and mid→last for a step; concat into preview_path."""
    workflow_id = workflow_id or "wan22_i2v"
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        first = f.keyframe_first_path
        mid = f.keyframe_mid_path
        last = f.keyframe_last_path
        beat = f.visual_prompt or f.description or ""
        premise = p.premise or ""
        if not (first and mid and last):
            raise ValueError("frame needs first, mid, and last keyframes first")

    first_p = _resolve_media_file(first)
    mid_p = _resolve_media_file(mid)
    _resolve_media_file(last)  # ensure end keyframe exists on disk

    clip_a = await _i2v_clip_from_image(
        project_id=project_id,
        frame_id=frame_id,
        start_image=first_p,
        prompt=build_transition_prompt(
            premise=premise, start_prompt=f"start: {beat}", end_prompt=f"midpoint: {beat}"
        ),
        label="clip_a",
        num_frames=num_frames,
        seed=frame_id * 17 + 11,
        workflow_id=workflow_id,
    )
    clip_b = await _i2v_clip_from_image(
        project_id=project_id,
        frame_id=frame_id,
        start_image=mid_p,
        prompt=build_transition_prompt(
            premise=premise, start_prompt=f"midpoint: {beat}", end_prompt=f"end: {beat}"
        ),
        label="clip_b",
        num_frames=num_frames,
        seed=frame_id * 17 + 13,
        workflow_id=workflow_id,
    )

    settings = get_settings()
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    preview = media / f"step_preview_f{frame_id}.mp4"
    # Prefer frame-extract concat for codec safety across Comfy outputs
    raw_a = media / "_clip_a_frames"
    raw_b = media / "_clip_b_frames"
    fa = extract_frames_from_video(clip_a, raw_a)
    fb = extract_frames_from_video(clip_b, raw_b)
    if fa and fb:
        seq = media / "_step_seq"
        concat_frame_dirs([raw_a, raw_b], seq)
        encode_frames_to_mp4(seq, preview, fps=settings.default_fps)
    else:
        concat_videos([clip_a, clip_b], preview)

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        fr.preview_path = str(preview)
        db.commit()

    return {
        "frame_id": frame_id,
        "kind": "step_clips",
        "preview_path": str(preview),
        "clip_a": str(clip_a),
        "clip_b": str(clip_b),
        # mid/last available for cross-step bridges
        "keyframe_last_path": last,
    }


async def generate_all_step_clips(
    project_id: int,
    *,
    skip_existing: bool = True,
    num_frames: int = 33,
) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {
                "id": f.id,
                "preview_path": f.preview_path,
                "ready": bool(
                    f.keyframe_first_path and f.keyframe_mid_path and f.keyframe_last_path
                ),
            }
            for f in sorted(p.frames, key=lambda x: x.position)
        ]
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    for fr in frames:
        if not fr["ready"]:
            errors.append({"frame_id": fr["id"], "error": "missing keyframes"})
            continue
        if skip_existing and fr.get("preview_path"):
            results.append({"frame_id": fr["id"], "skipped": True})
            skipped += 1
            continue
        try:
            results.append(
                await generate_step_clips(
                    project_id, fr["id"], num_frames=num_frames
                )
            )
        except Exception as e:
            errors.append({"frame_id": fr["id"], "error": str(e)})
    return {
        "project_id": project_id,
        "generated": len([r for r in results if not r.get("skipped")]),
        "skipped": skipped,
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }
