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
        prompt = f.visual_prompt or f.description

    if kind == "still":
        workflow_id = workflow_id or "still_hero"
        params = {
            "positive_prompt": prompt,
            "negative_prompt": "blurry, watermark, text",
            "seed": frame_id * 17,
            "filename_prefix": f"local_video/p{project_id}_f{frame_id}_still",
        }
    else:
        workflow_id = workflow_id or "wan22_t2v"
        validate_frame_count(num_frames)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": "blurry, watermark, text, static",
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
