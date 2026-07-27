from __future__ import annotations

from typing import Any

from app.db.models import Project, SessionLocal
from app.services import llm


def set_story(project_id: int, story: str, approved: bool = False) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        p.story = story
        p.story_approved = approved
        db.commit()
        return {"id": p.id, "story": p.story, "story_approved": p.story_approved}


async def generate_story(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        title, genre, premise = p.title, p.genre, p.premise
    story = await llm.generate_story(title, genre, premise)
    return set_story(project_id, story, approved=False)


async def extend_story(project_id: int, instruction: str) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        current = p.story or p.premise
    story = await llm.extend_story(current, instruction)
    return set_story(project_id, story, approved=False)


def approve_story(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        p.story_approved = True
        db.commit()
        return {"id": p.id, "story_approved": True}
