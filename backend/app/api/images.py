"""Generic image generation REST API (parity with MCP generate_image)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import images as images_svc

router = APIRouter(prefix="/images", tags=["images"])


class GenerateImageIn(BaseModel):
    prompt: str
    negative_prompt: str = ""
    seed: int | None = None
    width: int = 1024
    height: int = 576
    steps: int = 20
    cfg: float = 5.0
    workflow_id: str | None = None
    reference_image_path: str | None = None
    project_id: int | None = None
    label: str = "gen"
    preserve_style: bool = True


@router.post("/generate")
async def generate_image(body: GenerateImageIn):
    try:
        return await images_svc.generate_image(
            body.prompt,
            negative_prompt=body.negative_prompt,
            seed=body.seed,
            width=body.width,
            height=body.height,
            steps=body.steps,
            cfg=body.cfg,
            workflow_id=body.workflow_id,
            reference_image_path=body.reference_image_path,
            project_id=body.project_id,
            label=body.label,
            preserve_style=body.preserve_style,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e
