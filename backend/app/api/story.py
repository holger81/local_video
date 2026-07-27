from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import story as story_svc

router = APIRouter(prefix="/projects/{project_id}/story", tags=["story"])


class StoryIn(BaseModel):
    story: str
    approved: bool = False


class ExtendIn(BaseModel):
    instruction: str


@router.post("/generate")
async def generate_story(project_id: int):
    try:
        return await story_svc.generate_story(project_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/extend")
async def extend_story(project_id: int, body: ExtendIn):
    try:
        return await story_svc.extend_story(project_id, body.instruction)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.put("")
def set_story(project_id: int, body: StoryIn):
    try:
        return story_svc.set_story(project_id, body.story, body.approved)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/approve")
def approve_story(project_id: int):
    try:
        return story_svc.approve_story(project_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
