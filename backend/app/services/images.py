"""Generic ComfyUI image generation (not tied to a storyboard frame)."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.comfyui import ComfyUIClient
from app.services.workflows import apply_params


def _resolve_reference(path: str | Path) -> Path:
    """Resolve a reference image under MEDIA_DIR (absolute or media-relative)."""
    settings = get_settings()
    media_root = settings.media_dir.resolve()
    raw = Path(path)
    candidate = raw.resolve() if raw.is_absolute() else (media_root / raw).resolve()
    if not str(candidate).startswith(str(media_root)):
        raise ValueError("reference_image_path must be under MEDIA_DIR")
    if not candidate.is_file():
        raise FileNotFoundError(f"reference image not found: {path}")
    return candidate


async def generate_image(
    prompt: str,
    *,
    negative_prompt: str = "",
    seed: int | None = None,
    width: int = 1024,
    height: int = 576,
    steps: int = 20,
    cfg: float = 5.0,
    workflow_id: str | None = None,
    reference_image_path: str | None = None,
    project_id: int | None = None,
    label: str = "gen",
    preserve_style: bool = True,
) -> dict[str, Any]:
    """Generate a still via ComfyUI.

    Text-to-image uses ``still_hero``. When ``reference_image_path`` is set, uses
    ``still_edit`` (Flux ReferenceLatent) with the prompt as the edit instruction.

    When editing with a reference, ``preserve_style=False`` allows restyling
    (e.g. apply project visual style) instead of locking the reference art style.
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt is required")
    if width < 64 or height < 64:
        raise ValueError("width and height must be >= 64")

    settings = get_settings()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    token = secrets.token_hex(3)
    if project_id is not None:
        out_dir = (
            settings.media_dir / "projects" / str(project_id) / "generated" / stamp
        )
        prefix = f"local_video/p{project_id}_gen_{label}_{stamp}_{token}"
    else:
        out_dir = settings.media_dir / "generated" / stamp
        prefix = f"local_video/gen_{label}_{stamp}_{token}"
    out_dir.mkdir(parents=True, exist_ok=True)

    use_ref = bool((reference_image_path or "").strip())
    wf = workflow_id or ("still_edit" if use_ref else "still_hero")
    neg = (negative_prompt or "").strip() or (
        "blurry, watermark, logo, text overlay, collage, multi-panel, split screen, "
        "low quality"
    )
    noise_seed = int(seed) if seed is not None else secrets.randbelow(2**31)

    comfy = ComfyUIClient()
    params: dict[str, Any] = {
        "positive_prompt": text,
        "negative_prompt": neg,
        "seed": noise_seed,
        "filename_prefix": prefix,
        "width": int(width),
        "height": int(height),
        "steps": int(steps),
        "cfg": float(cfg),
    }

    uploaded: str | None = None
    if use_ref:
        ref = _resolve_reference(reference_image_path or "")
        uploaded = await comfy.upload_image(ref)
        # still_edit expects an edit-style instruction; keep the user prompt as-is.
        if preserve_style:
            style_bit = (
                "Preserve identity and art style from the reference unless asked "
                "to change them."
            )
        else:
            style_bit = (
                "Preserve subject identity, pose, and composition from the reference. "
                "Do NOT preserve the reference art style — apply the style described "
                "in the instruction."
            )
        params["positive_prompt"] = (
            "Edit this image into one continuous shot. "
            f"Instruction: {text}. "
            f"{style_bit}"
        )

    graph = apply_params(
        wf,
        params,
        uploaded_image_name=uploaded,
    )
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")

    out = outputs[0]
    dest = out_dir / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    try:
        media_rel = str(dest.resolve().relative_to(settings.media_dir.resolve()))
    except ValueError:
        media_rel = str(dest)

    return {
        "kind": "image",
        "path": str(dest),
        "media_path": media_rel,
        "url": f"/api/media/{media_rel}",
        "prompt": text,
        "negative_prompt": neg,
        "seed": noise_seed,
        "width": int(width),
        "height": int(height),
        "steps": int(steps),
        "cfg": float(cfg),
        "workflow_id": wf,
        "reference_image_path": str(reference_image_path) if use_ref else None,
        "project_id": project_id,
        "prompt_id": prompt_id,
    }
