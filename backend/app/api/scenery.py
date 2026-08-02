from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import scenery as scenery_svc

router = APIRouter(prefix="/projects/{project_id}/scenery", tags=["scenery"])


class SceneryIn(BaseModel):
    name: str
    description: str = ""
    appearance_prompt: str = ""
    variants: list[dict] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    approved: bool = False


class SceneryPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    appearance_prompt: str | None = None
    variants: list[dict] | None = None
    aliases: list[str] | None = None
    position: int | None = None
    approved: bool | None = None
    reference_image_path: str | None = None


class ReferenceIn(BaseModel):
    instruction: str | None = None


class MediaPathIn(BaseModel):
    media_path: str


@router.get("")
def list_scenery(project_id: int):
    try:
        return scenery_svc.list_scenery(project_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("")
def create_scenery(project_id: int, body: SceneryIn):
    try:
        return scenery_svc.create_scenery(
            project_id,
            name=body.name,
            description=body.description,
            appearance_prompt=body.appearance_prompt,
            variants=body.variants,
            aliases=body.aliases,
            approved=body.approved,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/{scenery_id}")
def patch_scenery(project_id: int, scenery_id: int, body: SceneryPatch):
    try:
        return scenery_svc.update_scenery(
            project_id, scenery_id, **body.model_dump(exclude_unset=True)
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{scenery_id}")
def delete_scenery(project_id: int, scenery_id: int):
    try:
        return scenery_svc.delete_scenery(project_id, scenery_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{scenery_id}/reference")
async def generate_reference(
    project_id: int, scenery_id: int, body: ReferenceIn | None = None
):
    body = body or ReferenceIn()
    try:
        return await scenery_svc.generate_reference(
            project_id, scenery_id, instruction=body.instruction
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/{scenery_id}/reference/from-media")
def reference_from_media(project_id: int, scenery_id: int, body: MediaPathIn):
    try:
        return scenery_svc.set_scenery_reference_from_media(
            project_id, scenery_id, body.media_path
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{scenery_id}/reference")
def delete_reference(project_id: int, scenery_id: int):
    try:
        return scenery_svc.delete_reference(project_id, scenery_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{scenery_id}/variants/{variant_id}/reference")
async def generate_variant_reference(
    project_id: int,
    scenery_id: int,
    variant_id: str,
    body: ReferenceIn | None = None,
):
    body = body or ReferenceIn()
    try:
        return await scenery_svc.generate_variant_reference(
            project_id, scenery_id, variant_id, instruction=body.instruction
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/{scenery_id}/variants/{variant_id}/reference/from-media")
def variant_reference_from_media(
    project_id: int, scenery_id: int, variant_id: str, body: MediaPathIn
):
    try:
        return scenery_svc.set_variant_reference_from_media(
            project_id, scenery_id, variant_id, body.media_path
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
