from __future__ import annotations

import asyncio
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


def _empty_keyframe(index: int, t_sec: float, role: str, prompt: str = "", path: str | None = None) -> dict[str, Any]:
    return {
        "index": index,
        "t_sec": float(t_sec),
        "role": role,
        "image_prompt": prompt or "",
        "path": path,
    }


def _legacy_keyframes_from_columns(f: StoryboardFrame) -> list[dict[str, Any]]:
    """Build a series from old first/mid/last columns when keyframes JSON is empty."""
    out: list[dict[str, Any]] = []
    if f.keyframe_first_path or (f.keyframe_first_prompt or "").strip():
        out.append(
            _empty_keyframe(0, 0.0, "first", f.keyframe_first_prompt or "", f.keyframe_first_path)
        )
    if f.keyframe_mid_path or (f.keyframe_mid_prompt or "").strip():
        out.append(
            _empty_keyframe(
                len(out),
                2.0,
                "middle",
                f.keyframe_mid_prompt or "",
                f.keyframe_mid_path,
            )
        )
    if f.keyframe_last_path or (f.keyframe_last_prompt or "").strip():
        dur = float(f.duration_hint_sec or 4.0)
        out.append(
            _empty_keyframe(
                len(out),
                dur,
                "last",
                f.keyframe_last_prompt or "",
                f.keyframe_last_path,
            )
        )
    for i, kf in enumerate(out):
        kf["index"] = i
    return out


def _keyframes_list(f: StoryboardFrame) -> list[dict[str, Any]]:
    raw = getattr(f, "keyframes", None) or []
    if isinstance(raw, list) and raw:
        out = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "middle")
            if i == 0:
                role = "first"
            out.append(
                _empty_keyframe(
                    i,
                    float(item.get("t_sec") or 0.0),
                    role,
                    str(item.get("image_prompt") or item.get("prompt") or ""),
                    item.get("path"),
                )
            )
        if out:
            out[-1]["role"] = "last" if len(out) > 1 else out[-1]["role"]
            if len(out) == 1:
                out[0]["role"] = "first"
            return out
    return _legacy_keyframes_from_columns(f)


def _sync_legacy_keyframe_columns(f: StoryboardFrame, keyframes: list[dict[str, Any]]) -> None:
    """Keep first/mid/last columns in sync for movie/continuity helpers."""
    f.keyframes = keyframes
    first = keyframes[0] if keyframes else None
    last = keyframes[-1] if keyframes else None
    middles = [k for k in keyframes if k.get("role") == "middle"]
    mid = middles[len(middles) // 2] if middles else None
    f.keyframe_first_path = (first or {}).get("path")
    f.keyframe_last_path = (last or {}).get("path")
    f.keyframe_mid_path = (mid or {}).get("path")
    f.keyframe_first_prompt = (first or {}).get("image_prompt") or ""
    f.keyframe_last_prompt = (last or {}).get("image_prompt") or ""
    f.keyframe_mid_prompt = (mid or {}).get("image_prompt") or ""


def _frame_dict(f: StoryboardFrame) -> dict[str, Any]:
    keyframes = _keyframes_list(f)
    return {
        "id": f.id,
        "position": f.position,
        "description": f.description,
        "visual_prompt": f.visual_prompt,
        "still_path": f.still_path,
        "keyframes": keyframes,
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


def _keyframes_ready(keyframes: list[dict[str, Any]]) -> bool:
    return bool(keyframes) and all((k.get("path") or "").strip() for k in keyframes)


async def rebuild_frame_keyframe_prompts(project_id: int, frame_id: int) -> dict[str, Any]:
    """LLM-plan a variable keyframe series (≤2s spacing). Keeps existing paths when prompts only."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        description = f.description or ""
        visual = f.visual_prompt or f.description or ""
        duration = float(f.duration_hint_sec or 4.0)
        is_new = bool(f.is_new_shot)
        prev_last_prompt = None
        if not is_new and idx > 0:
            prev_kfs = _keyframes_list(frames[idx - 1])
            if prev_kfs:
                prev_last_prompt = prev_kfs[-1].get("image_prompt") or None
        existing_paths = {
            i: (kf.get("path") or None) for i, kf in enumerate(_keyframes_list(f))
        }

    planned = await llm.plan_keyframe_series(
        description=description,
        visual=visual,
        duration_sec=duration,
        is_new_shot=is_new,
        prev_last_prompt=prev_last_prompt,
    )
    keyframes = planned["keyframes"]
    for kf in keyframes:
        # Preserve rendered path if slot index still exists (best-effort)
        if kf["index"] in existing_paths and existing_paths[kf["index"]]:
            kf["path"] = existing_paths[kf["index"]]

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        _sync_legacy_keyframe_columns(fr, keyframes)
        db.commit()
        db.refresh(fr)
        return _frame_dict(fr)


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
                    keyframes=[],
                )
            )
        db.commit()
    # Plan prompts per frame (LLM). Failures leave empty keyframes for later rebuild.
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        assert p is not None
        frame_ids = [f.id for f in sorted(p.frames, key=lambda x: x.position)]
    for fid in frame_ids:
        try:
            await rebuild_frame_keyframe_prompts(project_id, fid)
        except Exception:
            continue
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
        "keyframes",
    }
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "keyframes":
                if not isinstance(v, list):
                    raise ValueError("keyframes must be a list")
                normalized = []
                for i, item in enumerate(v):
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role") or "middle")
                    if i == 0:
                        role = "first"
                    normalized.append(
                        _empty_keyframe(
                            i,
                            float(item.get("t_sec") or 0.0),
                            role,
                            str(item.get("image_prompt") or item.get("prompt") or ""),
                            item.get("path"),
                        )
                    )
                if normalized:
                    normalized[-1]["role"] = "last" if len(normalized) > 1 else normalized[0]["role"]
                _sync_legacy_keyframe_columns(f, normalized)
            else:
                setattr(f, k, v)
        db.commit()
        db.refresh(f)
        return _frame_dict(f)


def delete_frame_media(project_id: int, frame_id: int, kind: str) -> dict[str, Any]:
    """Clear still, preview, keyframe:N, or legacy keyframe_* kind."""
    settings = get_settings()
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")

        old: str | None = None
        if kind in ("still", "preview"):
            path_attr = "still_path" if kind == "still" else "preview_path"
            old = getattr(f, path_attr)
            setattr(f, path_attr, None)
        elif kind.startswith("keyframe:"):
            idx = int(kind.split(":", 1)[1])
            keyframes = _keyframes_list(f)
            if idx < 0 or idx >= len(keyframes):
                raise ValueError(f"keyframe index {idx} out of range")
            old = keyframes[idx].get("path")
            keyframes[idx]["path"] = None
            _sync_legacy_keyframe_columns(f, keyframes)
        elif kind in ("keyframe_first", "keyframe_mid", "keyframe_last"):
            keyframes = _keyframes_list(f)
            role = {
                "keyframe_first": "first",
                "keyframe_mid": "middle",
                "keyframe_last": "last",
            }[kind]
            target = None
            if role == "first" and keyframes:
                target = 0
            elif role == "last" and keyframes:
                target = len(keyframes) - 1
            elif role == "middle":
                middles = [i for i, k in enumerate(keyframes) if k.get("role") == "middle"]
                target = middles[len(middles) // 2] if middles else None
            if target is None:
                raise ValueError(f"no {kind} on frame")
            old = keyframes[target].get("path")
            keyframes[target]["path"] = None
            _sync_legacy_keyframe_columns(f, keyframes)
        else:
            raise ValueError(
                "kind must be still, preview, keyframe:N, keyframe_first, "
                "keyframe_mid, or keyframe_last"
            )

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


def merge_keyframe_prompt_with_edit(existing_prompt: str, instruction: str) -> str:
    """Keep keyframe generation prompt aligned with cumulative edit intent."""
    base = (existing_prompt or "").strip()
    instr = (instruction or "").strip()
    if not instr:
        raise ValueError("edit instruction is required")
    if not base:
        return instr

    lower_base = base.lower()
    lower_instr = instr.lower()
    if lower_instr in lower_base:
        return base

    marker = "Edit adjustments:"
    if marker in base:
        return f"{base}\n- {instr}"
    return f"{base}\n\n{marker}\n- {instr}"


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
    """Bridge this beat's end image into the next beat's start via Wan FLF2V."""
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
        cur_kfs = _keyframes_list(cur)
        nxt_kfs = _keyframes_list(nxt)
        # Prefer step keyframes when present: last of this step → first of next.
        start_stored = (
            (cur_kfs[-1].get("path") if cur_kfs else None)
            or cur.keyframe_last_path
            or cur.still_path
        )
        end_ref = (
            (nxt_kfs[0].get("path") if nxt_kfs else None)
            or nxt.keyframe_first_path
            or nxt.still_path
        )
        if not start_stored:
            raise ValueError(
                "current frame has no last keyframe or still — create keyframes/stills first"
            )
        if not end_ref:
            raise ValueError(
                "next frame has no first keyframe or still — create keyframes/stills first"
            )
        start_beat = (
            (cur_kfs[-1].get("image_prompt") if cur_kfs else None)
            or cur.visual_prompt
            or cur.description
            or ""
        )
        end_beat = (
            (nxt_kfs[0].get("image_prompt") if nxt_kfs else None)
            or nxt.visual_prompt
            or nxt.description
            or ""
        )
        premise = p.premise or ""
        next_frame_id = nxt.id

    workflow_id = workflow_id or "wan22_flf2v"
    dest = await _bridge_clip_between_images(
        project_id=project_id,
        frame_id=frame_id,
        start_image=_resolve_media_file(start_stored),
        end_image=_resolve_media_file(end_ref),
        prompt=build_transition_prompt(
            premise=premise, start_prompt=start_beat, end_prompt=end_beat
        ),
        label="between",
        num_frames=num_frames,
        seed=frame_id * 17 + 3,
        workflow_id=workflow_id,
    )

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
        "workflow_id": workflow_id,
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
    index: int,
    role: str,
    prompt: str,
    source_path: str | Path | None,
    seed: int,
    force_edit: bool = False,
) -> Path:
    """T2I when no source; otherwise edit-from-previous (preferred for continuity)."""
    settings = get_settings()
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    comfy = ComfyUIClient()
    neg = still_negative_prompt(prompt)
    label = f"{index:02d}_{role}"

    if source_path:
        src = _resolve_media_file(str(source_path))
        uploaded = await comfy.upload_image(src)
        edit_prompt = build_edit_prompt(instruction=prompt, frame_prompt=prompt)
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{label}",
                "width": 1024,
                "height": 576,
                "steps": 20,
                "cfg": 5.0,
            },
            uploaded_image_name=uploaded,
        )
    else:
        if force_edit:
            raise ValueError("edit source required")
        graph = apply_params(
            "still_hero",
            {
                "positive_prompt": prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{label}",
            },
        )

    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError(f"ComfyUI produced no outputs for keyframe {label}")
    out = outputs[0]
    dest = media / f"keyframe_{label}_{out['filename']}"
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


async def generate_frame_keyframes(
    project_id: int,
    frame_id: int,
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Create a variable keyframe series: edit-chain; new shot starts fresh T2I."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        is_new = bool(f.is_new_shot)
        keyframes = _keyframes_list(f)
        prev_last_path = None
        if not is_new and idx > 0:
            prev_kfs = _keyframes_list(frames[idx - 1])
            if prev_kfs:
                prev_last_path = prev_kfs[-1].get("path")

    if not keyframes or not all((k.get("image_prompt") or "").strip() for k in keyframes):
        await rebuild_frame_keyframe_prompts(project_id, frame_id)
        with SessionLocal() as db:
            f = db.get(StoryboardFrame, frame_id)
            assert f
            keyframes = _keyframes_list(f)

    if skip_existing and _keyframes_ready(keyframes):
        return {
            "frame_id": frame_id,
            "kind": "keyframes",
            "generated": [],
            "skipped": [k["role"] for k in keyframes],
            "keyframes": keyframes,
            "keyframe_first_path": keyframes[0].get("path") if keyframes else None,
            "keyframe_last_path": keyframes[-1].get("path") if keyframes else None,
        }

    generated: list[int] = []
    skipped: list[int] = []
    last_path: str | None = None

    for i, kf in enumerate(keyframes):
        if skip_existing and (kf.get("path") or "").strip():
            skipped.append(i)
            last_path = kf.get("path")
            continue

        prompt = (kf.get("image_prompt") or "").strip()
        if not prompt:
            raise ValueError(f"frame {frame_id} keyframe {i} has no image_prompt")

        if i == 0:
            if is_new or not prev_last_path:
                source = None  # new shot / no prior → T2I (own series)
            else:
                source = prev_last_path
                prompt = (
                    f"{prompt} Continuation of the same shot — "
                    "preserve identity, wardrobe, and setting."
                )
        else:
            if not last_path:
                raise ValueError(
                    f"frame {frame_id} keyframe {i} needs previous keyframe image"
                )
            source = last_path

        dest = await _render_keyframe_image(
            project_id=project_id,
            frame_id=frame_id,
            index=i,
            role=str(kf.get("role") or "middle"),
            prompt=prompt,
            source_path=source,
            seed=frame_id * 31 + i * 97,
        )
        keyframes[i]["path"] = str(dest)
        last_path = str(dest)
        generated.append(i)

        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            _sync_legacy_keyframe_columns(fr, keyframes)
            db.commit()

    return {
        "frame_id": frame_id,
        "kind": "keyframes",
        "generated": generated,
        "skipped": skipped,
        "keyframes": keyframes,
        "keyframe_first_path": keyframes[0].get("path") if keyframes else None,
        "keyframe_last_path": keyframes[-1].get("path") if keyframes else None,
        "keyframe_mid_path": next(
            (k.get("path") for k in keyframes if k.get("role") == "middle"), None
        ),
    }


def _resolve_keyframe_index(keyframes: list[dict[str, Any]], phase_or_index: str | int) -> int:
    if isinstance(phase_or_index, int) or str(phase_or_index).isdigit():
        idx = int(phase_or_index)
        if idx < 0 or idx >= len(keyframes):
            raise ValueError(f"keyframe index {idx} out of range")
        return idx
    phase = str(phase_or_index)
    if phase in ("first", "start"):
        return 0
    if phase in ("last", "end"):
        return len(keyframes) - 1
    if phase in ("mid", "middle"):
        middles = [i for i, k in enumerate(keyframes) if k.get("role") == "middle"]
        if not middles:
            raise ValueError("no middle keyframe")
        return middles[len(middles) // 2]
    raise ValueError("phase must be first, mid, last, or a keyframe index")


async def generate_one_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Render one keyframe slot; edits from previous in-series image when available."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        is_new = bool(f.is_new_shot)
        keyframes = _keyframes_list(f)
        if not keyframes:
            raise ValueError("no keyframe series — rebuild prompts first")
        ki = _resolve_keyframe_index(keyframes, phase)
        prompt = (keyframes[ki].get("image_prompt") or "").strip()
        old_path = keyframes[ki].get("path")
        prev_path = keyframes[ki - 1].get("path") if ki > 0 else None
        prev_shot_last = None
        if ki == 0 and not is_new and idx > 0:
            prev_kfs = _keyframes_list(frames[idx - 1])
            if prev_kfs:
                prev_shot_last = prev_kfs[-1].get("path")

    if not prompt:
        raise ValueError(f"frame {frame_id} keyframe {ki} has no image_prompt")

    if ki == 0:
        source = None if (is_new or not prev_shot_last) else prev_shot_last
        if source:
            prompt = (
                f"{prompt} Continuation of the same shot — "
                "preserve identity, wardrobe, and setting."
            )
    else:
        source = prev_path
        if not source:
            raise ValueError("generate earlier keyframes first (edit chain)")

    dest = await _render_keyframe_image(
        project_id=project_id,
        frame_id=frame_id,
        index=ki,
        role=str(keyframes[ki].get("role") or "middle"),
        prompt=prompt,
        source_path=source,
        seed=seed if seed is not None else (frame_id * 31 + ki * 97),
    )
    if old_path:
        try:
            old = _resolve_media_file(old_path)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    keyframes[ki]["path"] = str(dest)
    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        _sync_legacy_keyframe_columns(fr, keyframes)
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
    """Prompt-edit an existing keyframe (or previous/still fallback) into that slot."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        keyframes = _keyframes_list(f)
        if not keyframes:
            raise ValueError("no keyframe series — rebuild prompts first")
        ki = _resolve_keyframe_index(keyframes, phase)
        keyframe_stored = keyframes[ki].get("path")
        prev_path = keyframes[ki - 1].get("path") if ki > 0 else None
        source_stored = keyframe_stored or prev_path or f.still_path
        if not source_stored:
            raise ValueError(
                f"frame has no keyframe {ki} or still to edit — generate one first"
            )
        effective_prompt = merge_keyframe_prompt_with_edit(
            str(keyframes[ki].get("image_prompt") or ""),
            instruction,
        )

    dest = await _render_keyframe_image(
        project_id=project_id,
        frame_id=frame_id,
        index=ki,
        role=str(keyframes[ki].get("role") or "middle"),
        prompt=effective_prompt,
        source_path=source_stored,
        seed=seed if seed is not None else (frame_id * 31 + 53 + ki),
        force_edit=True,
    )
    if keyframe_stored:
        try:
            old = _resolve_media_file(keyframe_stored)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    keyframes[ki]["path"] = str(dest)
    keyframes[ki]["image_prompt"] = effective_prompt
    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        _sync_legacy_keyframe_columns(fr, keyframes)
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


async def _bridge_clip_between_images(
    *,
    project_id: int,
    frame_id: int,
    start_image: Path,
    end_image: Path | None = None,
    prompt: str,
    label: str,
    num_frames: int,
    seed: int,
    workflow_id: str = "wan22_flf2v",
) -> Path:
    """Generate a clip locked to start (and optionally end) image via FLF2V or I2V."""
    if workflow_id == "wan22_flf2v":
        if end_image is None:
            raise ValueError("wan22_flf2v requires both start_image and end_image")
        return await _run_flf2v_two_pass(
            project_id=project_id,
            frame_id=frame_id,
            start_image=start_image,
            end_image=end_image,
            prompt=prompt,
            label=label,
            num_frames=num_frames,
            seed=seed,
        )

    settings = get_settings()
    validate_frame_count(num_frames)
    comfy = ComfyUIClient()
    start_name = await comfy.upload_image(start_image)
    uploads: dict[str, str] = {"start_image": start_name}
    if end_image is not None:
        uploads["end_image"] = await comfy.upload_image(end_image)
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
    graph = apply_params(workflow_id, params, uploaded_images=uploads)
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


async def _run_flf2v_two_pass(
    *,
    project_id: int,
    frame_id: int,
    start_image: Path,
    end_image: Path,
    prompt: str,
    label: str,
    num_frames: int,
    seed: int,
) -> Path:
    """FLF2V as high-noise then low-noise prompts with /free between (avoids dual-UNET crash)."""
    settings = get_settings()
    validate_frame_count(num_frames)
    comfy = ComfyUIClient()
    neg = (
        "blurry, watermark, text, static, jump cut, morphing face, flickering, "
        "collage, comic, storyboard, panels, grid, split screen, montage"
    )
    uploads = {
        "start_image": await comfy.upload_image(start_image),
        "end_image": await comfy.upload_image(end_image),
    }
    shared = {
        "positive_prompt": prompt,
        "negative_prompt": neg,
        "num_frames": num_frames,
        "width": settings.default_width,
        "height": settings.default_height,
    }

    # Pass 1: high-noise only → SaveLatent
    # Avoid POST /free on this ROCm host — it can kill the ComfyUI process.
    # Separate prompts still let Comfy unload the high UNET before loading low.
    high_params = {
        **shared,
        "seed": seed,
        "latent_prefix": f"latents/local_video/p{project_id}_f{frame_id}_{label}_high",
    }
    high_graph = apply_params("wan22_flf2v_high", high_params, uploaded_images=uploads)
    high_id = await comfy.queue_prompt(high_graph)
    high_hist = await comfy.wait_for_prompt(high_id)
    latents = [o for o in comfy.collect_outputs(high_hist) if o.get("kind") == "latents"]
    if not latents:
        # Some Comfy builds nest SaveLatent under outputs without a kind we expect.
        for _nid, node_out in (high_hist.get("outputs") or {}).items():
            for item in node_out.get("latents") or []:
                latents.append(
                    {
                        "kind": "latents",
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder") or "",
                        "type": item.get("type") or "output",
                    }
                )
    if not latents or not latents[0].get("filename"):
        raise RuntimeError("FLF2V high pass produced no latent output")
    latent_ref = comfy.latent_annotated_path(latents[0])

    # Brief pause so the high-noise model can leave GPU before low-noise loads.
    await asyncio.sleep(2)

    # Pass 2: low-noise + tiled decode → video
    low_params = {
        **shared,
        "fps": settings.default_fps,
        "filename_prefix": f"local_video/p{project_id}_f{frame_id}_{label}",
        "latent_file": latent_ref,
    }
    low_graph = apply_params("wan22_flf2v_low", low_params, uploaded_images=uploads)
    low_id = await comfy.queue_prompt(low_graph)
    low_hist = await comfy.wait_for_prompt(low_id)
    outputs = [
        o
        for o in comfy.collect_outputs(low_hist)
        if o.get("kind") in ("videos", "gifs", "images")
    ]
    if not outputs:
        raise RuntimeError("FLF2V low pass produced no video output")

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


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
    """Legacy single-image I2V helper (start frame only)."""
    return await _bridge_clip_between_images(
        project_id=project_id,
        frame_id=frame_id,
        start_image=start_image,
        end_image=None,
        prompt=prompt,
        label=label,
        num_frames=num_frames,
        seed=seed,
        workflow_id=workflow_id,
    )


async def generate_step_clips(
    project_id: int,
    frame_id: int,
    *,
    num_frames: int = 33,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """FLF2V between consecutive keyframes in the series; concat into preview_path."""
    workflow_id = workflow_id or "wan22_flf2v"
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        keyframes = _keyframes_list(f)
        beat = f.visual_prompt or f.description or ""
        premise = p.premise or ""
        if len(keyframes) < 2 or not _keyframes_ready(keyframes):
            raise ValueError("frame needs a complete keyframe series (at least first and last)")

    settings = get_settings()
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)

    clip_paths: list[Path] = []
    frame_dirs: list[Path] = []
    for i in range(len(keyframes) - 1):
        a = keyframes[i]
        b = keyframes[i + 1]
        start_p = _resolve_media_file(a["path"])
        end_p = _resolve_media_file(b["path"])
        clip = await _bridge_clip_between_images(
            project_id=project_id,
            frame_id=frame_id,
            start_image=start_p,
            end_image=end_p,
            prompt=build_transition_prompt(
                premise=premise,
                start_prompt=a.get("image_prompt") or f"t={a.get('t_sec')}s: {beat}",
                end_prompt=b.get("image_prompt") or f"t={b.get('t_sec')}s: {beat}",
            ),
            label=f"clip_{i:02d}",
            num_frames=num_frames,
            seed=frame_id * 17 + 11 + i,
            workflow_id=workflow_id,
        )
        clip_paths.append(clip)
        raw = media / f"_clip_{i:02d}_frames"
        extract_frames_from_video(clip, raw)
        frame_dirs.append(raw)

    preview = media / f"step_preview_f{frame_id}.mp4"
    if frame_dirs and all(any(d.glob("*.png")) for d in frame_dirs):
        seq = media / "_step_seq"
        concat_frame_dirs(frame_dirs, seq)
        encode_frames_to_mp4(seq, preview, fps=settings.default_fps)
    else:
        concat_videos(clip_paths, preview)

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        fr.preview_path = str(preview)
        db.commit()

    return {
        "frame_id": frame_id,
        "kind": "step_clips",
        "preview_path": str(preview),
        "clips": [str(c) for c in clip_paths],
        "keyframes": keyframes,
        "keyframe_last_path": keyframes[-1].get("path"),
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
                "ready": _keyframes_ready(_keyframes_list(f)),
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
