from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import movie as movie_svc
from app.services.workflows import list_workflows

router = APIRouter(tags=["jobs"])


class StartMovieIn(BaseModel):
    target_length_sec: float = 30.0
    format: str = "mp4"
    aspect: str = "16:9"
    chunk_frames: int | None = None
    overlap_frames: int | None = None
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    video_backend: str | None = None
    # frame_id or shot position → wan|ltx
    shot_backends: dict[str, str] | None = None
    t2v_workflow: str | None = None
    i2v_workflow: str | None = None
    flf2v_workflow: str | None = None
    prompt_base: str = ""
    negative_prompt: str = ""
    seed: int = 42


@router.get("/workflows")
def workflows():
    return list_workflows()


@router.post("/projects/{project_id}/movies")
async def start_movie(project_id: int, body: StartMovieIn):
    try:
        return await movie_svc.start_movie(project_id, **body.model_dump())
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.get("/jobs/{job_id}")
def get_job(job_id: int):
    try:
        return movie_svc.get_job_status(job_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/jobs/{job_id}/pause")
def pause_job(job_id: int):
    try:
        return movie_svc.pause_job(job_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/jobs/{job_id}/resume")
async def resume_job(job_id: int):
    try:
        return await movie_svc.resume_job(job_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    try:
        return movie_svc.cancel_job(job_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/projects/{project_id}/assets")
def list_assets(project_id: int):
    return movie_svc.list_assets(project_id)


@router.get("/jobs/{job_id}/movie")
def get_movie(job_id: int):
    try:
        return movie_svc.get_movie(job_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int):
    try:
        return movie_svc.delete_job(job_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
