from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import projects as projects_svc

router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectIn(BaseModel):
    title: str
    genre: str = ""
    premise: str = ""


class ProjectPatch(BaseModel):
    title: str | None = None
    genre: str | None = None
    visual_style: str | None = None
    premise: str | None = None
    video_backend: str | None = None


@router.get("")
def list_projects():
    return projects_svc.list_projects()


@router.post("")
def create_project(body: CreateProjectIn):
    return projects_svc.create_project(body.title, body.genre, body.premise)


@router.get("/{project_id}")
def get_project(project_id: int):
    try:
        return projects_svc.get_project(project_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.patch("/{project_id}")
def patch_project(project_id: int, body: ProjectPatch):
    try:
        return projects_svc.update_project(
            project_id, **body.model_dump(exclude_unset=True)
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
