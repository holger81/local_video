from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import storyboard as sb_svc

router = APIRouter(prefix="/projects/{project_id}/storyboard", tags=["storyboard"])


class ProposeIn(BaseModel):
    max_frames: int = 8


class FrameUpdate(BaseModel):
    description: str | None = None
    visual_prompt: str | None = None
    duration_hint_sec: float | None = None
    is_new_shot: bool | None = None
    position: int | None = None


class VisualIn(BaseModel):
    kind: str = "still"
    workflow_id: str | None = None
    num_frames: int = 33


class AllStillsIn(BaseModel):
    workflow_id: str | None = None
    skip_existing: bool = False


@router.post("/propose")
async def propose(project_id: int, body: ProposeIn | None = None):
    body = body or ProposeIn()
    try:
        return await sb_svc.propose_storyboard(project_id, body.max_frames)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/frames/{frame_id}")
def update_frame(project_id: int, frame_id: int, body: FrameUpdate):
    try:
        return sb_svc.update_frame(project_id, frame_id, **body.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/approve")
def approve(project_id: int):
    try:
        return sb_svc.approve_storyboard(project_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/stills")
async def create_all_stills(project_id: int, body: AllStillsIn | None = None):
    body = body or AllStillsIn()
    try:
        return await sb_svc.generate_all_stills(
            project_id,
            workflow_id=body.workflow_id,
            skip_existing=body.skip_existing,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/visual")
async def generate_visual(project_id: int, frame_id: int, body: VisualIn | None = None):
    body = body or VisualIn()
    try:
        return await sb_svc.generate_frame_visual(
            project_id,
            frame_id,
            kind=body.kind,
            workflow_id=body.workflow_id,
            num_frames=body.num_frames,
        )
    except Exception as e:
        raise HTTPException(500, str(e)) from e
