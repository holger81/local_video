from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol

from app.config import get_settings
from app.services.comfyui import ComfyUIClient
from app.services.workflows import WorkflowError, apply_params, validate_frame_count

BACKEND_WAN = "wan"
BACKEND_LTX2 = "ltx2"
BACKEND_LTX23 = "ltx23"
# Legacy product id — normalize to LTX-2 (19B).
BACKEND_LTX = "ltx"
VALID_BACKENDS = frozenset({BACKEND_WAN, BACKEND_LTX2, BACKEND_LTX23})
_BACKEND_ALIASES = {BACKEND_LTX: BACKEND_LTX2}
LTX_FAMILY = frozenset({BACKEND_LTX2, BACKEND_LTX23})

DEFAULT_NEG = (
    "blurry, watermark, text, static, jump cut, morphing face, flickering, "
    "collage, comic, storyboard, panels, grid, split screen, montage"
)

TIMELINE_NEG = (
    "blurry, oversaturated, pixelated, low resolution, grainy, distorted, noise, "
    "compression artifacts, jpeg artifacts, glitches, watermark, text, logo, "
    "signature, copyright, subtitles, distorted sound, saturated sound, loud"
)

_TIMELINE_COLORS = ("#4f8edc", "#e07b3a", "#5cb85c", "#d9534f")


def normalize_backend_id(name: str | None, *, default: str = BACKEND_WAN) -> str:
    raw = (name or default or BACKEND_WAN).strip().lower()
    raw = _BACKEND_ALIASES.get(raw, raw)
    if raw not in VALID_BACKENDS:
        raise ValueError(
            f"unknown video backend: {name!r} (expected wan|ltx2|ltx23; "
            f"'ltx' aliases to ltx2)"
        )
    return raw


def is_ltx_backend(name: str | None) -> bool:
    try:
        return normalize_backend_id(name) in LTX_FAMILY
    except ValueError:
        return False


def flf_safe_size(video_backend: str | None = None) -> tuple[int, int]:
    """Resolution for FLF beat animation — full default W×H often crashes ROCm.

    LTX-2 / LTX-2.3 prefer the trained bucket; Wan uses a moderated cap.
    """
    settings = get_settings()
    bid = normalize_backend_id(video_backend)
    if bid in LTX_FAMILY:
        return 768, 448
    w = min(int(settings.default_width or 1280), 832)
    h = min(int(settings.default_height or 704), 480)
    w -= w % 16
    h -= h % 16
    return max(w, 640), max(h, 384)


def timeline_safe_size(*, preview: bool = True) -> tuple[int, int]:
    """Skill Destiny timeline canvas (W/H divisible by 32)."""
    if preview:
        return 768, 512
    return 960, 544


def preferred_default_video_backend() -> str:
    """Settings default remains wan unless the operator overrides it."""
    return BACKEND_WAN


def ltx23_timeline_enabled() -> bool:
    """True when Settings opt-in is on and the timeline API graph is packaged."""
    settings = get_settings()
    if not bool(getattr(settings, "use_ltx23_timeline", False)):
        return False
    return _workflow_exists("ltx23_timeline")


def _div32(n: int, minimum: int = 256) -> int:
    n = max(minimum, int(n))
    return max(minimum, n - (n % 32))


def _dialog_lines(dialog: str) -> list[str]:
    lines: list[str] = []
    for raw in (dialog or "").splitlines():
        t = raw.strip()
        if not t:
            continue
        lines.append(t)
    if not lines and (dialog or "").strip():
        lines = [dialog.strip()]
    return lines


def pack_ltx23_timeline_segments(
    keyframes: list[dict[str, Any]],
    *,
    dialog: str = "",
    audio_notes: str = "",
    beat_prompt: str = "",
    fps: int = 24,
    max_segments: int = 4,
    preview: bool = True,
    max_total_frames: int = 121,
) -> dict[str, Any] | None:
    """Pack a keyframe series into up to 4 Skill Destiny timeline segments.

    Returns None when fewer than 2 ready keyframe paths exist.
    """
    from app.services.continuity import snap_frame_count

    ready = [
        kf
        for kf in keyframes
        if isinstance(kf, dict) and (kf.get("path") or "").strip()
    ]
    if len(ready) < 2:
        return None

    use = ready[: max(2, min(max_segments, len(ready)))]
    n = len(use)
    fps_v = max(1, int(fps or 24))

    # Per-segment durations from t_sec deltas; last uses a short hold.
    raw_lens: list[int] = []
    for i in range(n):
        t0 = float(use[i].get("t_sec") or 0.0)
        if i + 1 < n:
            t1 = float(use[i + 1].get("t_sec") or (t0 + 2.0))
            sec = max(0.75, t1 - t0)
        else:
            sec = 1.5
        raw_lens.append(max(8, int(round(sec * fps_v))))

    # Pad to 4 guide slots (repeat last image; tiny trailing lengths).
    while len(use) < max_segments:
        use.append(dict(use[-1]))
        raw_lens.append(8)

    total_raw = sum(raw_lens)
    target = snap_frame_count(
        min(total_raw, max_total_frames),
        minimum=17,
        maximum=max_total_frames,
        step=8,
    )
    # Scale lengths so sum == target (keep each ≥ 8).
    scale = target / max(1, total_raw)
    lengths = [max(8, int(round(x * scale))) for x in raw_lens]
    drift = target - sum(lengths)
    lengths[-1] = max(8, lengths[-1] + drift)
    # Re-snap if last adjust broke 8n+1 total.
    while (sum(lengths) - 1) % 8 != 0:
        lengths[-1] += 1

    speech = _dialog_lines(dialog)
    notes = (audio_notes or "").strip()
    beat = (beat_prompt or "").strip()
    prompts: list[str] = []
    for i in range(max_segments):
        visual = (
            (use[i].get("image_prompt") or "").strip()
            or beat
            or f"Cinematic beat segment {i + 1}."
        )
        bits = [visual]
        if i < n and i < len(speech):
            bits.append(speech[i])
        elif i == 0 and speech:
            bits.append(" ".join(speech))
        if i == 0 and notes:
            bits.append(f"Audio: {notes}")
        prompts.append(" ".join(bits))

    width, height = timeline_safe_size(preview=preview)
    width, height = _div32(width), _div32(height)
    latent_w, latent_h = _div32(width // 2), _div32(height // 2)
    idx2 = lengths[0]
    idx3 = lengths[0] + lengths[1]
    idx4 = lengths[0] + lengths[1] + lengths[2]
    total = sum(lengths)

    segments_meta = [
        {
            "prompt": prompts[i],
            "length": lengths[i],
            "color": _TIMELINE_COLORS[i % len(_TIMELINE_COLORS)],
            "path": (use[i].get("path") or "").strip(),
        }
        for i in range(max_segments)
    ]

    return {
        "paths": [s["path"] for s in segments_meta],
        "frames": lengths,
        "prompts": prompts,
        "local_prompts": " | ".join(prompts),
        "segment_lengths": ", ".join(str(x) for x in lengths),
        "timeline_data": json.dumps({"segments": segments_meta}),
        "num_frames": total,
        "idx_seg2": idx2,
        "idx_seg3": idx3,
        "idx_seg4": idx4,
        "width": width,
        "height": height,
        "latent_width": latent_w,
        "latent_height": latent_h,
        "fps": fps_v,
        "active_segments": n,
    }


async def release_comfy_vram(
    comfy: ComfyUIClient | None = None,
    *,
    unload_models: bool = False,
    pause_sec: float = 2.0,
) -> None:
    """Best-effort VRAM ease between Comfy jobs.

    With ``--disable-smart-memory``, models stay resident until
    ``unload_models=True``. Prefer reuse within the same graph; unload when
    swapping UNETs (Wan high→low) or finishing a beat.
    """
    client = comfy or ComfyUIClient()
    try:
        await client.free_memory(unload_models=unload_models)
    except Exception:
        pass
    if pause_sec > 0:
        await asyncio.sleep(pause_sec)


async def soft_release_comfy_vram(comfy: ComfyUIClient | None = None) -> None:
    """Free cached tensors but keep models loaded (reuse)."""
    await release_comfy_vram(comfy, unload_models=False, pause_sec=1.0)


async def unload_comfy_models(comfy: ComfyUIClient | None = None) -> None:
    """Unload models from GPU so the next prompt can load a different set."""
    await release_comfy_vram(comfy, unload_models=True, pause_sec=3.0)


def resolve_video_backend(
    *,
    handoff: dict[str, Any] | None = None,
    shot_backend: str | None = None,
    job_backend: str | None = None,
    project_backend: str | None = None,
    settings_default: str | None = None,
) -> str:
    """handoff → shot → job → project → settings → wan."""
    for candidate in (
        (handoff or {}).get("video_backend"),
        shot_backend,
        job_backend,
        project_backend,
        settings_default,
        BACKEND_WAN,
    ):
        if candidate is None or candidate == "":
            continue
        return normalize_backend_id(str(candidate))
    return BACKEND_WAN


class VideoBackend(Protocol):
    id: str

    def workflows(self) -> dict[str, str]:
        """Logical roles → workflow map ids (t2v, i2v, flf2v)."""

    def validate_num_frames(self, n: int) -> int: ...

    def flf2v_ready(self) -> bool:
        """True when FLF API graph/map can be loaded."""

    async def render_flf2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        start_image: Path,
        end_image: Path,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
    ) -> Path: ...

    async def render_i2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        start_image: Path,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
    ) -> Path: ...

    async def render_t2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
    ) -> Path: ...


def _workflow_exists(workflow_id: str) -> bool:
    from app.services.workflows import load_map, workflows_root

    try:
        meta = load_map(workflow_id)
    except WorkflowError:
        return False
    filename = meta.get("workflow") or f"{workflow_id}.json"
    return (workflows_root() / "api" / filename).is_file()


async def _download_first_output(
    comfy: ComfyUIClient,
    history: dict[str, Any],
    dest_dir: Path,
) -> Path:
    outputs = [
        o
        for o in comfy.collect_outputs(history)
        if o.get("kind") in ("videos", "gifs", "images") or o.get("filename")
    ]
    if not outputs:
        # Fall back to any collected output
        outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = dest_dir / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


async def _run_single_graph(
    *,
    workflow_id: str,
    params: dict[str, Any],
    uploaded_images: dict[str, str] | None,
    dest_dir: Path,
) -> Path:
    if not _workflow_exists(workflow_id):
        raise WorkflowError(
            f"workflow {workflow_id!r} is not available — import the API graph "
            f"into comfyui_workflows/api/ (see README video backends)"
        )
    comfy = ComfyUIClient()
    graph = apply_params(workflow_id, params, uploaded_images=uploaded_images or {})
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    return await _download_first_output(comfy, history, dest_dir)


class WanBackend:
    id = BACKEND_WAN

    def workflows(self) -> dict[str, str]:
        return {
            "t2v": "wan22_t2v",
            "i2v": "wan22_i2v",
            "flf2v": "wan22_flf2v",
        }

    def validate_num_frames(self, n: int) -> int:
        validate_frame_count(n)
        return n

    def flf2v_ready(self) -> bool:
        # Two-pass: need high + low maps/api
        return _workflow_exists("wan22_flf2v_high") and _workflow_exists("wan22_flf2v_low")

    async def render_flf2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        start_image: Path,
        end_image: Path,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
    ) -> Path:
        from app.services.storyboard import _run_flf2v_two_pass

        self.validate_num_frames(num_frames)
        return await _run_flf2v_two_pass(
            project_id=project_id,
            frame_id=frame_id,
            start_image=start_image,
            end_image=end_image,
            prompt=prompt,
            label=label,
            num_frames=num_frames,
            seed=seed,
            width=width,
            height=height,
            fps=fps,
            dest_dir=dest_dir,
            filename_prefix=filename_prefix,
            negative_prompt=negative_prompt,
        )

    async def render_i2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        start_image: Path,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
    ) -> Path:
        settings = get_settings()
        self.validate_num_frames(num_frames)
        comfy = ComfyUIClient()
        uploaded = await comfy.upload_image(start_image)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt or DEFAULT_NEG,
            "seed": seed,
            "num_frames": num_frames,
            "width": width if width is not None else settings.default_width,
            "height": height if height is not None else settings.default_height,
            "fps": fps if fps is not None else settings.default_fps,
            "steps": steps if steps is not None else settings.default_steps,
            "cfg": cfg if cfg is not None else settings.default_cfg,
            "sampler_name": sampler_name or settings.default_sampler,
            "scheduler": scheduler or settings.default_scheduler,
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=self.workflows()["i2v"],
            params=params,
            uploaded_images={"start_image": uploaded},
            dest_dir=media,
        )

    async def render_t2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
    ) -> Path:
        settings = get_settings()
        self.validate_num_frames(num_frames)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt or DEFAULT_NEG,
            "seed": seed,
            "num_frames": num_frames,
            "width": width if width is not None else settings.default_width,
            "height": height if height is not None else settings.default_height,
            "fps": fps if fps is not None else settings.default_fps,
            "steps": steps if steps is not None else settings.default_steps,
            "cfg": cfg if cfg is not None else settings.default_cfg,
            "sampler_name": sampler_name or settings.default_sampler,
            "scheduler": scheduler or settings.default_scheduler,
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=self.workflows()["t2v"],
            params=params,
            uploaded_images=None,
            dest_dir=media,
        )


class LtxFamilyBackend:
    """Shared LTX-2 / LTX-2.3 adapter — same contract as Wan; different workflow prefix."""

    def __init__(
        self,
        backend_id: str,
        *,
        workflow_prefix: str,
        label: str,
        with_ic_lora: bool = False,
    ) -> None:
        self.id = backend_id
        self._prefix = workflow_prefix
        self._label = label
        self._with_ic_lora = with_ic_lora

    def workflows(self) -> dict[str, str]:
        wfs = {
            "t2v": f"{self._prefix}_t2v",
            "i2v": f"{self._prefix}_i2v",
            "flf2v": f"{self._prefix}_flf2v",
        }
        if self._with_ic_lora:
            wfs["ic_lora"] = f"{self._prefix}_ic_lora"
        if self.id == BACKEND_LTX23:
            wfs["timeline"] = "ltx23_timeline"
        return wfs

    def validate_num_frames(self, n: int) -> int:
        # EmptyLTXVLatentVideo expects 8n+1 (subset of Wan's 4n+1).
        validate_frame_count(n, step=8)
        return n

    def flf2v_ready(self) -> bool:
        return _workflow_exists(self.workflows()["flf2v"])

    def ic_lora_ready(self) -> bool:
        wfs = self.workflows()
        if "ic_lora" not in wfs:
            return False
        return _workflow_exists(wfs["ic_lora"])

    def timeline_ready(self) -> bool:
        wfs = self.workflows()
        tid = wfs.get("timeline")
        return bool(tid and _workflow_exists(tid))

    async def render_timeline(
        self,
        *,
        project_id: int,
        frame_id: int,
        segment_paths: list[Path],
        local_prompts: str,
        segment_lengths: str,
        num_frames: int,
        frames_seg: list[int],
        idx_seg2: int,
        idx_seg3: int,
        idx_seg4: int,
        label: str = "timeline",
        seed: int = 42,
        width: int | None = None,
        height: int | None = None,
        latent_width: int | None = None,
        latent_height: int | None = None,
        fps: int | None = None,
        image_strength: float = 0.7,
        global_prompt: str = "",
        timeline_data: str = "",
        negative_prompt: str | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        lora_strength: float = 0.6,
    ) -> Path:
        """Skill Destiny 4-guide timeline render (Dual Character + AV 2-pass)."""
        settings = get_settings()
        if not self.timeline_ready():
            raise WorkflowError(
                f"{self._label} timeline workflow is not installed. Add "
                "api/ltx23_timeline.json (see docs/video-backends.md)."
            )
        if len(segment_paths) < 2:
            raise ValueError("timeline render needs at least 2 segment images")
        self.validate_num_frames(num_frames)
        fps_v = int(fps if fps is not None else settings.default_fps or 24)
        width_v = int(width if width is not None else 768)
        height_v = int(height if height is not None else 512)
        lat_w = int(latent_width if latent_width is not None else _div32(width_v // 2))
        lat_h = int(latent_height if latent_height is not None else _div32(height_v // 2))
        segs = list(frames_seg) + [8, 8, 8, 8]
        segs = segs[:4]
        paths = list(segment_paths)
        while len(paths) < 4:
            paths.append(paths[-1])

        comfy = ComfyUIClient()
        uploads: dict[str, str] = {}
        for i, path in enumerate(paths[:4], start=1):
            uploads[f"image_seg{i}"] = await comfy.upload_image(Path(path))

        params = {
            "local_prompts": local_prompts,
            "segment_lengths": segment_lengths,
            "timeline_data": timeline_data or "",
            "global_prompt": global_prompt or "",
            "negative_prompt": negative_prompt or TIMELINE_NEG,
            "seed": seed,
            "seed2": seed + 3,
            "num_frames": num_frames,
            "frames_seg1": segs[0],
            "frames_seg2": segs[1],
            "frames_seg3": segs[2],
            "frames_seg4": segs[3],
            "idx_seg2": idx_seg2,
            "idx_seg3": idx_seg3,
            "idx_seg4": idx_seg4,
            "width": width_v,
            "height": height_v,
            "latent_width": lat_w,
            "latent_height": lat_h,
            "fps": fps_v,
            "image_strength": float(image_strength),
            "lora_strength": float(lora_strength),
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=self.workflows()["timeline"],
            params=params,
            uploaded_images=uploads,
            dest_dir=media,
        )

    async def render_ic_lora(
        self,
        *,
        project_id: int,
        frame_id: int,
        reference_sheet: Path,
        prompt: str,
        label: str = "ic_lora",
        num_frames: int | None = None,
        duration_sec: float | None = None,
        seed: int = 42,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        start_image: Path | None = None,
        negative_prompt: str | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
    ) -> Path:
        """Reference-sheet Ingredients clip; width/height/duration are caller-flexible.

        Prefer the trained bucket (768×448, 121 frames, 24fps) for quality. For quick
        previews pass smaller size and shorter duration_sec (snapped to 8n+1 frames).
        """
        from app.services.continuity import snap_frame_count

        settings = get_settings()
        if not self.ic_lora_ready():
            raise WorkflowError(
                f"{self._label} IC-LoRA workflow is not installed. Add "
                f"api/{self._prefix}_ic_lora.json (see docs/video-backends.md)."
            )
        fps_v = int(fps if fps is not None else settings.default_fps or 24)
        if num_frames is None:
            if duration_sec is not None and float(duration_sec) > 0:
                # (duration * fps) + 1 matches EmptyLTXVLatentVideo length convention.
                raw = int(round(float(duration_sec) * fps_v)) + 1
                num_frames = snap_frame_count(
                    raw, minimum=9, maximum=121, step=8
                )
            else:
                num_frames = 121
        num_frames = self.validate_num_frames(num_frames)
        width_v = int(width if width is not None else 768)
        height_v = int(height if height is not None else 448)
        comfy = ComfyUIClient()
        uploads: dict[str, str] = {
            "reference_sheet": await comfy.upload_image(reference_sheet),
        }
        if start_image is not None:
            uploads["start_image"] = await comfy.upload_image(start_image)
        else:
            # Graph still has a LoadImage for start; reuse sheet when bypassing first frame.
            uploads["start_image"] = uploads["reference_sheet"]
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt
            or (
                "worst quality, inconsistent motion, blurry, jittery, distorted"
            ),
            "seed": seed,
            "num_frames": num_frames,
            "width": width_v,
            "height": height_v,
            "fps": fps_v,
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=self.workflows()["ic_lora"],
            params=params,
            uploaded_images=uploads,
            dest_dir=media,
        )

    async def render_flf2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        start_image: Path,
        end_image: Path,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
    ) -> Path:
        settings = get_settings()
        self.validate_num_frames(num_frames)
        wf = self.workflows()["flf2v"]
        if not self.flf2v_ready():
            raise WorkflowError(
                f"{self._label} FLF2V workflow is not installed. Export/import "
                f"{wf} into comfyui_workflows/api/ and maps/{wf}.yaml "
                f"(see docs/video-backends.md)."
            )
        comfy = ComfyUIClient()
        uploads = {
            "start_image": await comfy.upload_image(start_image),
            "end_image": await comfy.upload_image(end_image),
        }
        safe_w, safe_h = flf_safe_size(self.id)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt or DEFAULT_NEG,
            "seed": seed,
            "num_frames": num_frames,
            "width": width if width is not None else safe_w,
            "height": height if height is not None else safe_h,
            "fps": fps if fps is not None else settings.default_fps,
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=wf,
            params=params,
            uploaded_images=uploads,
            dest_dir=media,
        )

    async def render_i2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        start_image: Path,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
    ) -> Path:
        settings = get_settings()
        self.validate_num_frames(num_frames)
        comfy = ComfyUIClient()
        uploaded = await comfy.upload_image(start_image)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt or DEFAULT_NEG,
            "seed": seed,
            "num_frames": num_frames,
            "width": width if width is not None else settings.default_width,
            "height": height if height is not None else settings.default_height,
            "fps": fps if fps is not None else settings.default_fps,
            "steps": steps if steps is not None else settings.default_steps,
            "cfg": cfg if cfg is not None else settings.default_cfg,
            "sampler_name": sampler_name or settings.default_sampler,
            "scheduler": scheduler or settings.default_scheduler,
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=self.workflows()["i2v"],
            params=params,
            uploaded_images={"start_image": uploaded},
            dest_dir=media,
        )

    async def render_t2v(
        self,
        *,
        project_id: int,
        frame_id: int,
        prompt: str,
        label: str,
        num_frames: int,
        seed: int,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        dest_dir: Path | None = None,
        filename_prefix: str | None = None,
        negative_prompt: str | None = None,
        steps: int | None = None,
        cfg: float | None = None,
        sampler_name: str | None = None,
        scheduler: str | None = None,
    ) -> Path:
        settings = get_settings()
        self.validate_num_frames(num_frames)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt or DEFAULT_NEG,
            "seed": seed,
            "num_frames": num_frames,
            "width": width if width is not None else settings.default_width,
            "height": height if height is not None else settings.default_height,
            "fps": fps if fps is not None else settings.default_fps,
            "steps": steps if steps is not None else settings.default_steps,
            "cfg": cfg if cfg is not None else settings.default_cfg,
            "sampler_name": sampler_name or settings.default_sampler,
            "scheduler": scheduler or settings.default_scheduler,
            "filename_prefix": filename_prefix
            or f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        media = dest_dir or (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        return await _run_single_graph(
            workflow_id=self.workflows()["t2v"],
            params=params,
            uploaded_images=None,
            dest_dir=media,
        )


_BACKENDS: dict[str, VideoBackend] = {
    BACKEND_WAN: WanBackend(),
    BACKEND_LTX2: LtxFamilyBackend(
        BACKEND_LTX2,
        workflow_prefix="ltx2",
        label="LTX-2",
        with_ic_lora=False,
    ),
    BACKEND_LTX23: LtxFamilyBackend(
        BACKEND_LTX23,
        workflow_prefix="ltx23",
        label="LTX-2.3",
        with_ic_lora=True,
    ),
}


def get_video_backend(name: str | None = None) -> VideoBackend:
    bid = normalize_backend_id(name)
    return _BACKENDS[bid]


def list_video_backends() -> list[dict[str, Any]]:
    out = []
    for bid, backend in _BACKENDS.items():
        wfs = backend.workflows()
        entry: dict[str, Any] = {
            "id": bid,
            "workflows": wfs,
            "flf2v_ready": backend.flf2v_ready(),
            "t2v_ready": _workflow_exists(wfs["t2v"]),
            "i2v_ready": _workflow_exists(wfs["i2v"]),
        }
        if "ic_lora" in wfs:
            entry["ic_lora_ready"] = _workflow_exists(wfs["ic_lora"])
        if "timeline" in wfs:
            entry["timeline_ready"] = bool(
                getattr(backend, "timeline_ready", lambda: False)()
            )
        out.append(entry)
    return out
