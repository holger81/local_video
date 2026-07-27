from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Local Video Studio"
    data_dir: Path = Path("/data")
    media_dir: Path = Path("/media")
    workflows_dir: Path = Path("/app/comfyui_workflows")

    database_url: str = "sqlite:////data/local_video.db"
    redis_url: str = "redis://redis:6379"

    comfyui_base_url: str = "http://192.168.10.31:8188"
    llama_base_url: str = "http://192.168.10.31:9292/v1"
    llama_model: str = "LiquidAI/LFM2-2.6B-Exp-GGUF:Q4_K_M"
    llama_api_key: str = "not-needed"
    # Effective context budget for prompt truncation / max_tokens (0 = auto from model).
    llama_n_ctx: int = 0
    llama_max_tokens: int = 2048

    chunk_frames: int = 33
    overlap_frames: int = 12
    default_fps: int = 24
    default_width: int = 1280
    default_height: int = 704
    default_steps: int = 20
    default_cfg: float = 1.0
    default_sampler: str = "euler"
    default_scheduler: str = "simple"

    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8700

    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def _env_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    """Env defaults merged with optional runtime overlay under data_dir/app_settings.json."""
    base = _env_settings()
    # Local import avoids circular dependency at module load.
    from app.services.runtime_settings import load_overlay

    overlay = load_overlay(base.data_dir)
    if not overlay:
        return base
    return base.model_copy(update=overlay)
