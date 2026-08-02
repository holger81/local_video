"""Project-independent image library under MEDIA_DIR/library/."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Project, SessionLocal

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


def library_root() -> Path:
    root = get_settings().media_dir / "library"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _asset_dir(asset_id: str) -> Path:
    return library_root() / asset_id


def _meta_path(asset_id: str) -> Path:
    return _asset_dir(asset_id) / "meta.json"


def _safe_filename(name: str) -> str:
    base = Path(name or "upload.bin").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "upload.bin"
    return cleaned[:120]


def _media_rel(path: Path) -> str:
    settings = get_settings()
    try:
        return str(path.resolve().relative_to(settings.media_dir.resolve()))
    except ValueError:
        return str(path)


def _asset_dict(meta: dict[str, Any]) -> dict[str, Any]:
    rel = meta.get("media_path") or ""
    return {
        "id": meta.get("id"),
        "label": meta.get("label") or "",
        "filename": meta.get("filename") or "",
        "path": meta.get("path"),
        "media_path": rel,
        "url": f"/api/media/{rel}" if rel else None,
        "created_at": meta.get("created_at"),
        "derived_from": meta.get("derived_from"),
        "source": meta.get("source") or "upload",
    }


def _read_meta(asset_id: str) -> dict[str, Any]:
    path = _meta_path(asset_id)
    if not path.is_file():
        raise KeyError(f"library image {asset_id} not found")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise KeyError(f"library image {asset_id} not found")
    return data


def _write_meta(asset_id: str, meta: dict[str, Any]) -> None:
    _meta_path(asset_id).write_text(json.dumps(meta, indent=2) + "\n")


def project_style_text(project_id: int) -> str:
    """visual_style if set, else genre — used for style_lock_phrase / negatives."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        style = (getattr(p, "visual_style", None) or "").strip()
        if style:
            return style
        return (p.genre or "").strip()


def list_images() -> list[dict[str, Any]]:
    root = library_root()
    items: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not child.is_dir():
            continue
        meta_file = child / "meta.json"
        if not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and meta.get("id"):
            items.append(_asset_dict(meta))
    items.sort(key=lambda m: m.get("created_at") or "", reverse=True)
    return items


def get_image(asset_id: str) -> dict[str, Any]:
    return _asset_dict(_read_meta(asset_id))


def delete_image(asset_id: str) -> dict[str, Any]:
    meta = _read_meta(asset_id)
    folder = _asset_dir(asset_id)
    if folder.is_dir():
        shutil.rmtree(folder)
    return {"deleted": True, "id": asset_id, "media_path": meta.get("media_path")}


def upload_image(
    data: bytes,
    *,
    filename: str,
    label: str | None = None,
    derived_from: str | None = None,
    source: str = "upload",
) -> dict[str, Any]:
    if not data:
        raise ValueError("empty image data")
    asset_id = uuid.uuid4().hex[:12]
    folder = _asset_dir(asset_id)
    folder.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(filename)
    dest = folder / safe
    dest.write_bytes(data)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {
        "id": asset_id,
        "label": (label or Path(safe).stem).strip() or asset_id,
        "filename": safe,
        "path": str(dest),
        "media_path": _media_rel(dest),
        "created_at": stamp,
        "derived_from": derived_from,
        "source": source,
    }
    _write_meta(asset_id, meta)
    return _asset_dict(meta)


def upload_image_base64(
    b64: str,
    *,
    filename: str,
    label: str | None = None,
) -> dict[str, Any]:
    import base64

    raw = (b64 or "").strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception as e:
        raise ValueError(f"invalid base64 image: {e}") from e
    return upload_image(data, filename=filename, label=label, source="upload")


def ingest_file(
    src: Path,
    *,
    label: str | None = None,
    derived_from: str | None = None,
    source: str = "generated",
) -> dict[str, Any]:
    """Copy an existing MEDIA_DIR file into the library as a new asset."""
    data = src.read_bytes()
    return upload_image(
        data,
        filename=src.name,
        label=label or src.stem,
        derived_from=derived_from,
        source=source,
    )


async def transform_image(
    asset_id: str,
    instruction: str,
    *,
    seed: int | None = None,
    preserve_style: bool = True,
    negative_prompt: str = "",
    label: str | None = None,
) -> dict[str, Any]:
    """Edit a library image via still_edit; store result as a new library asset."""
    from app.services.images import generate_image

    meta = _read_meta(asset_id)
    src_path = meta.get("path") or ""
    text = (instruction or "").strip()
    if not text:
        raise ValueError("instruction is required")
    result = await generate_image(
        text,
        negative_prompt=negative_prompt,
        seed=seed,
        reference_image_path=src_path,
        label="lib_edit",
        preserve_style=preserve_style,
    )
    out_path = Path(result["path"])
    asset = ingest_file(
        out_path,
        label=label or f"{meta.get('label') or asset_id} (edit)",
        derived_from=asset_id,
        source="transform",
    )
    asset["prompt_id"] = result.get("prompt_id")
    asset["seed"] = result.get("seed")
    asset["source_transform"] = result
    return asset


async def apply_project_style(
    asset_id: str,
    project_id: int,
    *,
    instruction: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Restyle a library image to match a project's visual_style (or genre)."""
    from app.services.llm import style_negatives
    from app.services.storyboard import build_edit_prompt, still_negative_prompt

    style = project_style_text(project_id)
    if not style:
        raise ValueError(
            "project has no visual_style or genre — set one before applying style"
        )
    user_bit = (instruction or "").strip()
    instr = (
        "Restyle this image to match the project's visual style. "
        "Keep subject identity, pose, and composition; change art medium, lighting, "
        "and rendering style to match the project."
    )
    if user_bit:
        instr = f"{instr} Additional direction: {user_bit}"
    edit_prompt = build_edit_prompt(
        instruction=instr,
        frame_prompt="",
        cast_sheet="",
        genre=style,
    )
    neg = ", ".join(
        x
        for x in (
            still_negative_prompt(instr),
            style_negatives(style),
        )
        if x
    )
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        title = (p.title if p else "") or f"project {project_id}"
    return await transform_image(
        asset_id,
        edit_prompt,
        seed=seed,
        preserve_style=False,
        negative_prompt=neg,
        label=f"{title} style",
    )


def resolve_media_path(stored: str) -> Path:
    """Resolve absolute or MEDIA_DIR-relative path (incl. library/...)."""
    from app.services.storyboard import _resolve_media_file

    return _resolve_media_file(stored)


def copy_media_into(
    media_path: str,
    dest_dir: Path,
    *,
    filename: str | None = None,
) -> Path:
    """Copy a media/library file into dest_dir; return destination path."""
    src = resolve_media_path(media_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_filename(filename or src.name)
    # Avoid clobber: unique suffix
    stem, suffix = Path(name).stem, Path(name).suffix
    dest = dest_dir / name
    if dest.exists():
        dest = dest_dir / f"{stem}_{secrets.token_hex(3)}{suffix}"
    shutil.copy2(src, dest)
    return dest
