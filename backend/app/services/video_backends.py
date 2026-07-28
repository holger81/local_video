from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from app.config import get_settings
from app.services.comfyui import ComfyUIClient
from app.services.workflows import WorkflowError, apply_params, validate_frame_count

BACKEND_WAN = "wan"
BACKEND_LTX = "ltx"
VALID_BACKENDS = frozenset({BACKEND_WAN, BACKEND_LTX})

DEFAULT_NEG = (
    "blurry, watermark, text, static, jump cut, morphing face, flickering, "
    "collage, comic, storyboard, panels, grid, split screen, montage"
)


def normalize_backend_id(name: str | None, *, default: str = BACKEND_WAN) -> str:
    raw = (name or default or BACKEND_WAN).strip().lower()
    if raw not in VALID_BACKENDS:
        raise ValueError(f"unknown video backend: {name!r} (expected wan|ltx)")
    return raw


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


class LtxBackend:
    """LTX adapter — same contract as Wan; requires imported ltx_* API graphs."""

    id = BACKEND_LTX

    def workflows(self) -> dict[str, str]:
        return {
            "t2v": "ltx_t2v",
            "i2v": "ltx_i2v",
            "flf2v": "ltx_flf2v",
            "ic_lora": "ltx_ic_lora",
        }

    def validate_num_frames(self, n: int) -> int:
        # LTX EmptyLTXVLatentVideo expects 8n+1 (subset of Wan's 4n+1).
        validate_frame_count(n, step=8)
        return n

    def flf2v_ready(self) -> bool:
        return _workflow_exists(self.workflows()["flf2v"])

    def ic_lora_ready(self) -> bool:
        return _workflow_exists(self.workflows()["ic_lora"])

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
                "LTX IC-LoRA workflow is not installed. Add api/ltx_ic_lora.json "
                "(see docs/video-backends.md)."
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
                "LTX FLF2V workflow is not installed. Export/import ltx_flf2v into "
                "comfyui_workflows/api/ and maps/ltx_flf2v.yaml (see docs/video-backends.md)."
            )
        comfy = ComfyUIClient()
        uploads = {
            "start_image": await comfy.upload_image(start_image),
            "end_image": await comfy.upload_image(end_image),
        }
        params = {
            "positive_prompt": prompt,
            "negative_prompt": negative_prompt or DEFAULT_NEG,
            "seed": seed,
            "num_frames": num_frames,
            "width": width if width is not None else settings.default_width,
            "height": height if height is not None else settings.default_height,
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
    BACKEND_LTX: LtxBackend(),
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
        out.append(entry)
    return out
