from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import jobs, projects, story, storyboard
from app.api import settings as settings_api
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
app.include_router(settings_api.router, prefix="/api")

_STATIC = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)


@app.get("/api/health")
async def health():
    s = get_settings()
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
        "llama_base_url": s.llama_base_url,
        "llama_model": s.llama_model,
        "llama_n_ctx": s.llama_n_ctx,
        "frontend_built": (_STATIC / "index.html").is_file(),
    }


@app.get("/api/media/{path:path}")
def media_file(path: str):
    full = (settings.media_dir / path).resolve()
    if not str(full).startswith(str(settings.media_dir.resolve())):
        raise HTTPException(400, "invalid path")
    if not full.exists() or not full.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(full)


def _index_html() -> FileResponse:
    index = _STATIC / "index.html"
    if not index.is_file():
        raise HTTPException(404, "frontend not built")
    return FileResponse(index)


# Built frontend: assets + SPA fallback so /projects/1 reloads work
if _STATIC.is_dir():
    assets = _STATIC / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    def frontend_root():
        return _index_html()

    @app.get("/{full_path:path}")
    def frontend_spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        candidate = (_STATIC / full_path).resolve()
        if str(candidate).startswith(str(_STATIC.resolve())) and candidate.is_file():
            return FileResponse(candidate)
        return _index_html()
