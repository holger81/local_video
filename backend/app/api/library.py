"""Global image library REST API."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.services import library as lib_svc

router = APIRouter(prefix="/library", tags=["library"])


class TransformIn(BaseModel):
    instruction: str
    seed: int | None = None
    preserve_style: bool = True


class ApplyStyleIn(BaseModel):
    project_id: int
    instruction: str | None = None
    seed: int | None = None


@router.get("")
def list_library():
    return lib_svc.list_images()


@router.post("/upload")
async def upload_library_image(
    file: UploadFile = File(...),
    label: str | None = Form(None),
):
    data = await file.read()
    try:
        return lib_svc.upload_image(
            data,
            filename=file.filename or "upload.bin",
            label=label,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{asset_id}/transform")
async def transform_library_image(asset_id: str, body: TransformIn):
    try:
        return await lib_svc.transform_image(
            asset_id,
            body.instruction,
            seed=body.seed,
            preserve_style=body.preserve_style,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/{asset_id}/apply-project-style")
async def apply_project_style(asset_id: str, body: ApplyStyleIn):
    try:
        return await lib_svc.apply_project_style(
            asset_id,
            body.project_id,
            instruction=body.instruction,
            seed=body.seed,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.get("/{asset_id}")
def get_library_image(asset_id: str):
    try:
        return lib_svc.get_image(asset_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.delete("/{asset_id}")
def delete_library_image(asset_id: str):
    try:
        return lib_svc.delete_image(asset_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
