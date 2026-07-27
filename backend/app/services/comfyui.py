from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import websockets

from app.config import get_settings


class ComfyUIError(RuntimeError):
    pass


class ComfyUIClient:
    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.comfyui_base_url).rstrip("/")
        self.client_id = str(uuid.uuid4())

    async def health(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base_url}/system_stats")
            r.raise_for_status()
            return r.json()

    async def upload_image(self, path: Path, overwrite: bool = True) -> str:
        """Upload a local image to ComfyUI input folder. Returns filename used in workflows."""
        data = {"overwrite": "true" if overwrite else "false"}
        async with httpx.AsyncClient(timeout=120.0) as client:
            with path.open("rb") as f:
                files = {"image": (path.name, f, "image/png")}
                r = await client.post(f"{self.base_url}/upload/image", data=data, files=files)
            r.raise_for_status()
            payload = r.json()
            return payload.get("name") or path.name

    async def queue_prompt(self, prompt: dict[str, Any]) -> str:
        body = {"prompt": prompt, "client_id": self.client_id}
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{self.base_url}/prompt", json=body)
            if r.status_code >= 400:
                raise ComfyUIError(f"queue failed: {r.status_code} {r.text}")
            data = r.json()
            if "error" in data:
                raise ComfyUIError(str(data["error"]))
            return data["prompt_id"]

    async def get_history(self, prompt_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{self.base_url}/history/{prompt_id}")
            r.raise_for_status()
            return r.json()

    async def wait_for_prompt(
        self, prompt_id: str, timeout_sec: float = 3600.0, poll_sec: float = 2.0
    ) -> dict[str, Any]:
        """Wait via WebSocket when possible, fall back to history polling."""
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws?clientId={self.client_id}"
        deadline = asyncio.get_event_loop().time() + timeout_sec

        try:
            async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(30.0, remaining))
                    except TimeoutError:
                        hist = await self.get_history(prompt_id)
                        if prompt_id in hist:
                            return hist[prompt_id]
                        continue
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    data = msg.get("data") or {}
                    if mtype == "executing" and data.get("node") is None and data.get("prompt_id") == prompt_id:
                        hist = await self.get_history(prompt_id)
                        if prompt_id in hist:
                            return hist[prompt_id]
                    if mtype == "execution_error" and data.get("prompt_id") == prompt_id:
                        raise ComfyUIError(json.dumps(data))
        except Exception:
            # Fall back to polling
            pass

        while asyncio.get_event_loop().time() < deadline:
            hist = await self.get_history(prompt_id)
            if prompt_id in hist:
                return hist[prompt_id]
            await asyncio.sleep(poll_sec)
        raise ComfyUIError(f"timeout waiting for prompt {prompt_id}")

    async def download_view(
        self, filename: str, dest: Path, subfolder: str = "", folder_type: str = "output"
    ) -> Path:
        params = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(f"{self.base_url}/view", params=params)
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return dest

    async def free_memory(self, *, unload_models: bool = True) -> None:
        """Ask ComfyUI to unload models and free VRAM/RAM (POST /free)."""
        body = {"unload_models": unload_models, "free_memory": True}
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{self.base_url}/free", json=body)
            r.raise_for_status()

    def collect_outputs(self, history_entry: dict[str, Any]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for _node_id, node_out in (history_entry.get("outputs") or {}).items():
            for key in ("images", "gifs", "videos", "latents"):
                for item in node_out.get(key) or []:
                    outputs.append(
                        {
                            "kind": key,
                            "filename": item.get("filename"),
                            "subfolder": item.get("subfolder") or "",
                            "type": item.get("type") or "output",
                        }
                    )
        return outputs

    def latent_annotated_path(self, item: dict[str, Any]) -> str:
        """Build a LoadLatent path that reads a file from ComfyUI output/."""
        filename = item.get("filename") or ""
        subfolder = (item.get("subfolder") or "").strip().strip("/")
        rel = f"{subfolder}/{filename}" if subfolder else filename
        # annotated_filepath strips trailing " [output]" (9 chars including space)
        return f"{rel} [output]"
