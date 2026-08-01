#!/usr/bin/env bash
# Apply AMD/ROCm SaveVideo audio fix before starting ComfyUI, then exec.
# ComfyUI bug: audio tensors stay on GPU; .numpy() without .cpu() crashes ROCm.
# Safe to re-run: only patches the unfixed pattern.
set -euo pipefail

COMFYUI_DIR="${COMFYUI_DIR:-/workload/ComfyUI}"
TARGET="${COMFYUI_DIR}/comfy_api/latest/_input_impl/video_types.py"

if [[ -f "${TARGET}" ]] && grep -q '\.float()\.numpy()' "${TARGET}"; then
  echo "[entrypoint-amd] patching AMD audio SaveVideo bug in ${TARGET}"
  sed -i 's/\.float()\.numpy()/.float().cpu().numpy()/g' "${TARGET}"
elif [[ -f "${TARGET}" ]]; then
  echo "[entrypoint-amd] ${TARGET} already patched or uses .cpu() — skipping"
else
  echo "[entrypoint-amd] WARN: ${TARGET} not found — skipping audio patch"
fi

exec "$@"
