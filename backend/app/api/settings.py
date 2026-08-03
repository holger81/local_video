from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services import runtime_settings as rs

router = APIRouter(tags=["settings"])


class SettingsUpdate(BaseModel):
    llama_base_url: str | None = None
    llama_model: str | None = None
    llama_api_key: str | None = None
    llama_n_ctx: int | None = Field(default=None, ge=0)
    llama_max_tokens: int | None = Field(default=None, ge=64, le=128000)
    comfyui_base_url: str | None = None
    default_video_backend: str | None = None
    use_ltx23_timeline: bool | None = None


@router.get("/settings")
def get_app_settings():
    return rs.settings_public(get_settings())


@router.put("/settings")
async def update_app_settings(body: SettingsUpdate):
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return rs.settings_public(get_settings())

    # When picking a model without an explicit ctx, pull it from the server list.
    if "llama_model" in updates and "llama_n_ctx" not in updates:
        try:
            listed = await list_llm_models()
            match = next(
                (m for m in listed["models"] if m["id"] == updates["llama_model"]),
                None,
            )
            if match and match.get("n_ctx"):
                updates["llama_n_ctx"] = int(match["n_ctx"])
        except HTTPException:
            pass

    if "default_video_backend" in updates:
        from app.services.video_backends import normalize_backend_id

        try:
            updates["default_video_backend"] = normalize_backend_id(
                updates["default_video_backend"]
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    rs.save_overlay(updates)
    return rs.settings_public(get_settings())


@router.get("/video-backends")
def list_video_backends():
    from app.services.video_backends import list_video_backends as list_backends
    from app.services.video_backends import ltx23_timeline_enabled

    settings = get_settings()
    return {
        "default": settings.default_video_backend or "wan",
        "use_ltx23_timeline": bool(getattr(settings, "use_ltx23_timeline", False)),
        "ltx23_timeline_enabled": ltx23_timeline_enabled(),
        "backends": list_backends(),
    }


@router.get("/llm/models")
async def list_llm_models():
    settings = get_settings()
    base = settings.llama_base_url.rstrip("/")
    url = f"{base}/models"
    headers = {}
    if settings.llama_api_key:
        headers["Authorization"] = f"Bearer {settings.llama_api_key}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Failed to list models from {url}: {e}") from e

    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise HTTPException(502, "Unexpected /models response from LLM server")

    models = [rs.normalize_llm_model(item) for item in raw if isinstance(item, dict)]
    models = [m for m in models if m.get("id")]
    models.sort(
        key=lambda m: (0 if m.get("status") == "loaded" else 1, m["id"].lower())
    )
    return {
        "base_url": settings.llama_base_url,
        "selected": settings.llama_model,
        "llama_n_ctx": settings.llama_n_ctx,
        "llama_max_tokens": settings.llama_max_tokens,
        "models": models,
    }
