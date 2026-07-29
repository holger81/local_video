from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import characters as char_svc

router = APIRouter(prefix="/projects/{project_id}/characters", tags=["characters"])


class CharacterIn(BaseModel):
    name: str
    description: str = ""
    appearance_prompt: str = ""
    outfits: list[dict] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    approved: bool = False
    intro_frame_id: int | None = None


class CharacterPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    appearance_prompt: str | None = None
    outfits: list[dict] | None = None
    aliases: list[str] | None = None
    position: int | None = None
    approved: bool | None = None
    intro_frame_id: int | None = None


class DetectIn(BaseModel):
    replace_auto: bool = False


class ReferenceIn(BaseModel):
    instruction: str | None = None


@router.get("")
def list_characters(project_id: int):
    try:
        return char_svc.list_characters(project_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("")
def create_character(project_id: int, body: CharacterIn):
    try:
        return char_svc.create_character(
            project_id,
            name=body.name,
            description=body.description,
            appearance_prompt=body.appearance_prompt,
            outfits=body.outfits,
            aliases=body.aliases,
            approved=body.approved,
            intro_frame_id=body.intro_frame_id,
            auto_detected=False,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/{character_id}")
def patch_character(project_id: int, character_id: int, body: CharacterPatch):
    try:
        return char_svc.update_character(
            project_id, character_id, **body.model_dump(exclude_unset=True)
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{character_id}")
def delete_character(project_id: int, character_id: int):
    try:
        return char_svc.delete_character(project_id, character_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/detect")
async def detect_characters(project_id: int, body: DetectIn | None = None):
    body = body or DetectIn()
    try:
        return await char_svc.detect_characters(
            project_id, replace_auto=body.replace_auto
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{character_id}/reference")
async def generate_reference(
    project_id: int, character_id: int, body: ReferenceIn | None = None
):
    body = body or ReferenceIn()
    try:
        return await char_svc.generate_reference(
            project_id, character_id, instruction=body.instruction
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{character_id}/reference")
def delete_reference(project_id: int, character_id: int):
    try:
        return char_svc.delete_reference(project_id, character_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/{character_id}/outfits/{outfit_id}/reference")
async def generate_outfit_reference(
    project_id: int,
    character_id: int,
    outfit_id: str,
    body: ReferenceIn | None = None,
):
    body = body or ReferenceIn()
    try:
        return await char_svc.generate_outfit_reference(
            project_id,
            character_id,
            outfit_id,
            instruction=body.instruction,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(400, str(e)) from e
