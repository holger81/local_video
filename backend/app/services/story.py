from __future__ import annotations

import logging
from typing import Any

from app.db.models import Project, SessionLocal
from app.services import llm

log = logging.getLogger(__name__)


def set_story(project_id: int, story: str, approved: bool = False) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        p.story = story
        p.story_approved = approved
        db.commit()
        return {"id": p.id, "story": p.story, "story_approved": p.story_approved}


async def _maybe_detect_cast(project_id: int) -> None:
    try:
        from app.services import characters as char_svc

        await char_svc.detect_characters(project_id, replace_auto=False)
    except Exception as e:
        log.warning("auto cast detect failed for project %s: %s", project_id, e)


async def generate_story(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        title, genre, premise = p.title, p.genre, p.premise
    story = await llm.generate_story(title, genre, premise)
    result = set_story(project_id, story, approved=False)
    await _maybe_detect_cast(project_id)
    return result


async def extend_story(project_id: int, instruction: str) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        current = p.story or p.premise
    story = await llm.extend_story(current, instruction)
    result = set_story(project_id, story, approved=False)
    await _maybe_detect_cast(project_id)
    return result


async def approve_story(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        p.story_approved = True
        db.commit()
        payload = {"id": p.id, "story_approved": True}
    await _maybe_detect_cast(project_id)
    return payload
