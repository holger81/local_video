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
    mcp_port: int = 8090

    cors_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
