from __future__ import annotations

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


def build_visual_prompt(
    *,
    story: str,
    title: str,
    genre: str,
    frame_prompt: str,
    frame_position: int,
    total_frames: int,
    prev_prompt: str | None = None,
    next_prompt: str | None = None,
) -> str:
    """Compose an image prompt that locks overall story continuity."""
    story_bit = _truncate(story or "", 700)
    frame_bit = _truncate(frame_prompt or "", 400)
    parts: list[str] = []
    header = []
    if title:
        header.append(f'Title "{title}"')
    if genre:
        header.append(f"genre {genre}")
    if header:
        parts.append(", ".join(header) + ".")
    if story_bit:
        parts.append(f"Overall story (keep characters, wardrobe, setting, tone consistent): {story_bit}")
    parts.append(
        f"Storyboard frame {frame_position + 1} of {max(total_frames, 1)} — depict this beat: {frame_bit}"
    )
    if prev_prompt:
        parts.append(f"Continues after: {_truncate(prev_prompt, 160)}")
    if next_prompt:
        parts.append(f"Leads toward: {_truncate(next_prompt, 160)}")
    parts.append(
        "Same film, same cast and world as the overall story; cohesive cinematic look across the storyboard."
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
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next((i for i, fr in enumerate(frames) if fr.id == frame_id), 0)
        prev_prompt = None
        next_prompt = None
        if idx > 0:
            prev_prompt = frames[idx - 1].visual_prompt or frames[idx - 1].description
        if idx + 1 < len(frames):
            next_prompt = frames[idx + 1].visual_prompt or frames[idx + 1].description
        prompt = build_visual_prompt(
            story=p.story or p.premise or "",
            title=p.title or "",
            genre=p.genre or "",
            frame_prompt=f.visual_prompt or f.description or "",
            frame_position=f.position if f.position is not None else idx,
            total_frames=len(frames),
            prev_prompt=prev_prompt,
            next_prompt=next_prompt,
        )

    if kind == "still":
        workflow_id = workflow_id or "still_hero"
        params = {
            "positive_prompt": prompt,
            "negative_prompt": (
                "blurry, watermark, text overlay, logo, inconsistent characters, "
                "different person each frame, style change, collage"
            ),
            "seed": frame_id * 17,
            "filename_prefix": f"local_video/p{project_id}_f{frame_id}_still",
        }
    else:
        workflow_id = workflow_id or "wan22_t2v"
        validate_frame_count(num_frames)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": (
                "blurry, watermark, text, static, inconsistent characters, style change"
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


async def generate_all_stills(
    project_id: int,
    *,
    workflow_id: str | None = None,
    skip_existing: bool = False,
) -> dict[str, Any]:
    """Generate a still for every storyboard frame (sequential; one ComfyUI job at a time)."""
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
