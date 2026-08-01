from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings


class WorkflowError(RuntimeError):
    pass


def workflows_root() -> Path:
    return get_settings().workflows_dir


def list_workflows() -> list[dict[str, Any]]:
    maps_dir = workflows_root() / "maps"
    if not maps_dir.exists():
        return []
    items = []
    for path in sorted(maps_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        items.append(
            {
                "id": path.stem,
                "workflow": data.get("workflow"),
                "description": data.get("description", ""),
                "fields": list((data.get("fields") or {}).keys()),
                "model_files": data.get("model_files") or [],
            }
        )
    return items


def load_map(workflow_id: str) -> dict[str, Any]:
    path = workflows_root() / "maps" / f"{workflow_id}.yaml"
    if not path.exists():
        raise WorkflowError(f"unknown workflow map: {workflow_id}")
    return yaml.safe_load(path.read_text()) or {}


def load_api_workflow(workflow_id: str) -> dict[str, Any]:
    meta = load_map(workflow_id)
    filename = meta.get("workflow") or f"{workflow_id}.json"
    path = workflows_root() / "api" / filename
    if not path.exists():
        raise WorkflowError(f"missing API workflow: {path}")
    return json.loads(path.read_text())


def apply_params(
    workflow_id: str,
    params: dict[str, Any],
    *,
    uploaded_image_name: str | None = None,
    uploaded_images: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a parameterized API prompt graph.

    uploaded_images maps field names (e.g. start_image, end_image) to ComfyUI filenames.
    uploaded_image_name is a shorthand for start_image.
    """
    meta = load_map(workflow_id)
    graph = copy.deepcopy(load_api_workflow(workflow_id))
    fields = meta.get("fields") or {}
    uploads = dict(uploaded_images or {})
    if uploaded_image_name and "start_image" not in uploads:
        uploads["start_image"] = uploaded_image_name

    for key, value in params.items():
        if key not in fields or value is None:
            continue
        if key in uploads:
            continue
        _apply_field(graph, workflow_id, fields[key], value)

    for key, filename in uploads.items():
        if key not in fields or not filename:
            continue
        _apply_field(graph, workflow_id, fields[key], filename)

    # CreateVideo.fps is FLOAT in current Comfy; PrimitiveInt links fail validation.
    _fix_create_video_fps(graph, params.get("fps"))
    # ROCm: large temporal windows (incl. defaults ≥ clip length) hard-crash decode.
    _clamp_vae_decode_tiled(graph)
    return graph


# Keep temporal_size under typical step-clip length so tiling actually chunks.
_SAFE_VAE_TILED = {
    "tile_size": 256,
    "overlap": 32,
    "temporal_size": 8,
    "temporal_overlap": 4,
}


def _clamp_vae_decode_tiled(graph: dict[str, Any]) -> None:
    """Force conservative VAEDecodeTiled settings on every queued graph."""
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "VAEDecodeTiled":
            continue
        inputs = node.setdefault("inputs", {})
        for key, value in _SAFE_VAE_TILED.items():
            inputs[key] = value


def _coerce_value_for_node(graph: dict[str, Any], node_id: str, input_name: str, value: Any) -> Any:
    cls = (graph.get(node_id) or {}).get("class_type") or ""
    if input_name == "fps" and cls == "CreateVideo":
        return float(value)
    if input_name == "value" and cls == "PrimitiveInt":
        return int(value)
    return value


def _fix_create_video_fps(graph: dict[str, Any], fps_param: Any) -> None:
    """Ensure CreateVideo.fps is a FLOAT widget value, not an INT link."""
    for node in graph.values():
        if node.get("class_type") != "CreateVideo":
            continue
        inputs = node.setdefault("inputs", {})
        fps = inputs.get("fps")
        if isinstance(fps, (list, tuple)) and fps:
            linked = graph.get(str(fps[0])) or {}
            if fps_param is not None:
                inputs["fps"] = float(fps_param)
            elif linked.get("class_type") == "PrimitiveInt":
                inputs["fps"] = float((linked.get("inputs") or {}).get("value", 24))
            else:
                inputs["fps"] = 24.0
        elif fps is not None:
            inputs["fps"] = float(fps)


def _apply_field(
    graph: dict[str, Any],
    workflow_id: str,
    spec: dict[str, Any],
    value: Any,
) -> None:
    targets = [spec, *(spec.get("also") or [])]
    for target in targets:
        node_id = str(target["node"])
        input_name = target["input"]
        if node_id not in graph:
            raise WorkflowError(f"node {node_id} missing in {workflow_id}")
        coerced = _coerce_value_for_node(graph, node_id, input_name, value)
        graph[node_id]["inputs"][input_name] = coerced

def validate_frame_count(n: int, *, step: int = 4) -> None:
    """Wan uses 4n+1; LTX Comfy graphs want 8n+1 (also satisfies 4n+1)."""
    if step < 1:
        raise WorkflowError(f"invalid frame step {step}")
    if n < step + 1 or (n - 1) % step != 0:
        raise WorkflowError(f"frame_count must be {step}n+1 (got {n})")
