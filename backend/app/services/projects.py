from __future__ import annotations

from typing import Any

from app.db.models import Project, SessionLocal


def _project_dict(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "genre": p.genre,
        "premise": p.premise,
        "story": p.story,
        "story_approved": p.story_approved,
        "storyboard_approved": p.storyboard_approved,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "frame_count": len(p.frames or []),
    }


def list_projects() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(Project).order_by(Project.id.desc()).all()
        return [_project_dict(p) for p in rows]


def create_project(title: str, genre: str = "", premise: str = "") -> dict[str, Any]:
    with SessionLocal() as db:
        p = Project(title=title, genre=genre or "", premise=premise or "")
        db.add(p)
        db.commit()
        db.refresh(p)
        return _project_dict(p)


def get_project(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        data = _project_dict(p)
        data["frames"] = [
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
        return data
