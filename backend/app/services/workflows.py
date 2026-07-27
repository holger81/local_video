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
) -> dict[str, Any]:
    """Return a parameterized API prompt graph."""
    meta = load_map(workflow_id)
    graph = copy.deepcopy(load_api_workflow(workflow_id))
    fields = meta.get("fields") or {}

    for key, value in params.items():
        if key not in fields or value is None:
            continue
        spec = fields[key]
        node_id = str(spec["node"])
        input_name = spec["input"]
        if node_id not in graph:
            raise WorkflowError(f"node {node_id} missing in {workflow_id}")
        if key == "start_image" and uploaded_image_name:
            graph[node_id]["inputs"][input_name] = uploaded_image_name
        else:
            graph[node_id]["inputs"][input_name] = value

    if uploaded_image_name and "start_image" in fields:
        spec = fields["start_image"]
        graph[str(spec["node"])]["inputs"][spec["input"]] = uploaded_image_name

    return graph


def validate_frame_count(n: int) -> None:
    if n < 5 or (n - 1) % 4 != 0:
        raise WorkflowError(f"frame_count must be 4n+1 (got {n})")
