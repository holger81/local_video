from __future__ import annotations

from typing import Any

from app.db.models import Project, SessionLocal


def _project_dict(p: Project) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "genre": p.genre,
        "visual_style": getattr(p, "visual_style", None) or "",
        "premise": p.premise,
        "story": p.story,
        "story_approved": p.story_approved,
        "storyboard_approved": p.storyboard_approved,
        "video_backend": getattr(p, "video_backend", None) or "wan",
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "frame_count": len(p.frames or []),
    }


def list_projects() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.query(Project).order_by(Project.id.desc()).all()
        return [_project_dict(p) for p in rows]


def create_project(title: str, genre: str = "", premise: str = "") -> dict[str, Any]:
    from app.config import get_settings

    settings = get_settings()
    with SessionLocal() as db:
        p = Project(
            title=title,
            genre=genre or "",
            premise=premise or "",
            video_backend=settings.default_video_backend or "wan",
        )
        db.add(p)
        db.commit()
        db.refresh(p)
        return _project_dict(p)


def update_project(project_id: int, **fields: Any) -> dict[str, Any]:
    allowed = {"title", "genre", "premise", "video_backend", "visual_style"}
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "video_backend":
                from app.services.video_backends import normalize_backend_id

                p.video_backend = normalize_backend_id(str(v))
            elif k == "visual_style":
                p.visual_style = str(v)
            else:
                setattr(p, k, v)
        db.commit()
        db.refresh(p)
        return _project_dict(p)


def get_project(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        data = _project_dict(p)
        data["frames"] = [_frame_dict_from_orm(f) for f in p.frames]
        from app.services.characters import _character_dict

        data["characters"] = [
            _character_dict(c) for c in sorted(p.characters, key=lambda x: x.position)
        ]
        return data


def _frame_dict_from_orm(f) -> dict[str, Any]:
    from app.services.storyboard import _frame_dict

    return _frame_dict(f)
