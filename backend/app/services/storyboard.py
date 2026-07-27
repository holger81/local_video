from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Project, SessionLocal, StoryboardFrame
from app.services import llm
from app.services.comfyui import ComfyUIClient
from app.services.ffmpeg import extract_frames_from_video
from app.services.workflows import apply_params, validate_frame_count


def _frames_payload(project_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        return [
            {
                "id": f.id,
                "position": f.position,
                "description": f.description,
                "visual_prompt": f.visual_prompt,
                "still_path": f.still_path,
                "preview_path": f.preview_path,
                "duration_hint_sec": f.duration_hint_sec,
                "is_new_shot": f.is_new_shot,
            }
            for f in p.frames
        ]


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
        return {
            "id": f.id,
            "position": f.position,
            "description": f.description,
            "visual_prompt": f.visual_prompt,
            "still_path": f.still_path,
            "preview_path": f.preview_path,
            "duration_hint_sec": f.duration_hint_sec,
            "is_new_shot": f.is_new_shot,
        }


def delete_frame_media(project_id: int, frame_id: int, kind: str) -> dict[str, Any]:
    """Clear still or preview path and remove the file if it lives under media_dir."""
    if kind not in ("still", "preview"):
        raise ValueError("kind must be 'still' or 'preview'")
    settings = get_settings()
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        path_attr = "still_path" if kind == "still" else "preview_path"
        old = getattr(f, path_attr)
        setattr(f, path_attr, None)
        db.commit()
        db.refresh(f)
        payload = {
            "id": f.id,
            "position": f.position,
            "description": f.description,
            "visual_prompt": f.visual_prompt,
            "still_path": f.still_path,
            "preview_path": f.preview_path,
            "duration_hint_sec": f.duration_hint_sec,
            "is_new_shot": f.is_new_shot,
            "deleted": kind,
        }

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
        # If video, also extract first frame as still
        if out["kind"] in ("gifs", "videos") or dest.suffix.lower() in {".mp4", ".webm", ".gif"}:
            frames_dir = media / "extracted"
            frames = extract_frames_from_video(dest, frames_dir)
            if frames:
                still = media / "still.png"
                still.write_bytes(frames[0].read_bytes())
                with SessionLocal() as db:
                    fr = db.get(StoryboardFrame, frame_id)
                    assert fr
                    fr.preview_path = str(dest)
                    fr.still_path = str(still)
                    db.commit()
                return {
                    "frame_id": frame_id,
                    "kind": kind,
                    "preview_path": str(dest),
                    "still_path": str(still),
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
