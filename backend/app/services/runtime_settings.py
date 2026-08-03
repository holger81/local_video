from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import Settings, _env_settings

OVERLAY_KEYS = (
    "llama_base_url",
    "llama_model",
    "llama_api_key",
    "llama_n_ctx",
    "llama_max_tokens",
    "comfyui_base_url",
    "default_video_backend",
    "use_ltx23_timeline",
)


def overlay_path(data_dir: Path | None = None) -> Path:
    root = data_dir or _env_settings().data_dir
    return Path(root) / "app_settings.json"


def load_overlay(data_dir: Path | None = None) -> dict[str, Any]:
    path = overlay_path(data_dir)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in OVERLAY_KEYS:
        if key not in raw:
            continue
        val = raw[key]
        if key in ("llama_n_ctx", "llama_max_tokens"):
            if val is None or val == "":
                continue
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                continue
        elif key == "use_ltx23_timeline":
            if isinstance(val, bool):
                out[key] = val
            elif isinstance(val, (int, float)):
                out[key] = bool(val)
            elif isinstance(val, str):
                out[key] = val.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(val, str):
            stripped = val.strip()
            if stripped:
                out[key] = stripped
        elif val is not None:
            out[key] = val
    return out


def save_overlay(
    updates: dict[str, Any], data_dir: Path | None = None
) -> dict[str, Any]:
    path = overlay_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_overlay(data_dir)
    for key in OVERLAY_KEYS:
        if key not in updates:
            continue
        val = updates[key]
        if val is None or val == "":
            current.pop(key, None)
            continue
        if key in ("llama_n_ctx", "llama_max_tokens"):
            current[key] = int(val)
        elif key == "use_ltx23_timeline":
            if isinstance(val, bool):
                current[key] = val
            elif isinstance(val, str):
                current[key] = val.strip().lower() in ("1", "true", "yes", "on")
            else:
                current[key] = bool(val)
        else:
            current[key] = str(val).strip()
    path.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return current


def settings_public(s: Settings) -> dict[str, Any]:
    return {
        "llama_base_url": s.llama_base_url,
        "llama_model": s.llama_model,
        "llama_api_key_set": bool(s.llama_api_key) and s.llama_api_key != "not-needed",
        "llama_n_ctx": s.llama_n_ctx,
        "llama_max_tokens": s.llama_max_tokens,
        "comfyui_base_url": s.comfyui_base_url,
        "default_video_backend": s.default_video_backend or "wan",
        "use_ltx23_timeline": bool(getattr(s, "use_ltx23_timeline", False)),
        "overlay_path": str(overlay_path(s.data_dir)),
    }


def _parse_ctx_size_from_args(args: list[Any] | None) -> int | None:
    if not args:
        return None
    for i, item in enumerate(args):
        text = str(item)
        if text in ("--ctx-size", "-c") and i + 1 < len(args):
            try:
                return int(args[i + 1])
            except (TypeError, ValueError):
                return None
        if text.startswith("--ctx-size="):
            try:
                return int(text.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _parse_ctx_size_from_preset(preset: str | None) -> int | None:
    if not preset:
        return None
    match = re.search(r"(?m)^\s*ctx-size\s*=\s*(\d+)\s*$", preset)
    if match:
        return int(match.group(1))
    return None


def normalize_llm_model(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten llama.cpp / OpenAI-compatible model entries with context sizes."""
    model_id = str(item.get("id") or item.get("model") or "").strip()
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    arch = (
        item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
    )

    n_ctx_loaded = meta.get("n_ctx")
    n_ctx_train = meta.get("n_ctx_train")
    ctx_configured = _parse_ctx_size_from_args(
        status.get("args")
    ) or _parse_ctx_size_from_preset(status.get("preset"))
    # Prefer live loaded ctx, then configured ctx-size, then train ctx.
    n_ctx = None
    for candidate in (n_ctx_loaded, ctx_configured, n_ctx_train):
        try:
            if candidate is not None and int(candidate) > 0:
                n_ctx = int(candidate)
                break
        except (TypeError, ValueError):
            continue

    modalities = arch.get("input_modalities") or ["text"]
    return {
        "id": model_id,
        "status": status.get("value") or item.get("status") or "unknown",
        "n_ctx": n_ctx,
        "n_ctx_loaded": int(n_ctx_loaded) if n_ctx_loaded else None,
        "n_ctx_configured": ctx_configured,
        "n_ctx_train": int(n_ctx_train) if n_ctx_train else None,
        "n_params": meta.get("n_params"),
        "size_bytes": meta.get("size"),
        "ftype": meta.get("ftype"),
        "input_modalities": modalities,
        "source": item.get("source"),
        "owned_by": item.get("owned_by"),
    }
