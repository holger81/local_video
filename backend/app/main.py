from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import jobs, projects, story, storyboard
from app.config import get_settings
from app.db.models import init_db
from app.services.comfyui import ComfyUIClient

settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router, prefix="/api")
app.include_router(story.router, prefix="/api")
app.include_router(storyboard.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)


@app.get("/api/health")
async def health():
    comfy_ok = False
    comfy_info = None
    try:
        comfy_info = await ComfyUIClient().health()
        comfy_ok = True
    except Exception as e:
        comfy_info = {"error": str(e)}
    return {
        "status": "ok",
        "comfyui": {"ok": comfy_ok, "info": comfy_info},
        "llama_base_url": settings.llama_base_url,
    }


@app.get("/api/media/{path:path}")
def media_file(path: str):
    full = (settings.media_dir / path).resolve()
    if not str(full).startswith(str(settings.media_dir.resolve())):
        raise HTTPException(400, "invalid path")
    if not full.exists() or not full.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(full)


# Serve built frontend if present
_static = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _static.exists():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="frontend")
