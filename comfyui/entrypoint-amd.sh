#!/usr/bin/env bash
# Apply AMD/ROCm SaveVideo audio fix before starting ComfyUI, then exec.
# ComfyUI bug: audio tensors stay on GPU; .numpy() without .cpu() crashes ROCm.
# Safe to re-run: only patches the unfixed pattern.
set -euo pipefail

REL="comfy_api/latest/_input_impl/video_types.py"
CANDIDATES=()

if [[ -n "${COMFYUI_DIR:-}" ]]; then
  CANDIDATES+=("${COMFYUI_DIR}/${REL}")
fi
# Live box reports /workload/ComfyUI; some images use /root/ComfyUI.
CANDIDATES+=(
  "/workload/ComfyUI/${REL}"
  "/root/ComfyUI/${REL}"
  "/ComfyUI/${REL}"
  "${HOME}/ComfyUI/${REL}"
)

TARGET=""
for candidate in "${CANDIDATES[@]}"; do
  if [[ -f "${candidate}" ]]; then
    TARGET="${candidate}"
    break
  fi
done

if [[ -z "${TARGET}" ]]; then
  # Last resort: shallow find under common roots (keeps startup cheap).
  for root in /workload/ComfyUI /root/ComfyUI /ComfyUI; do
    if [[ -d "${root}" ]]; then
      found="$(find "${root}" -path "*/comfy_api/latest/_input_impl/video_types.py" 2>/dev/null | head -n1 || true)"
      if [[ -n "${found}" ]]; then
        TARGET="${found}"
        break
      fi
    fi
  done
fi

if [[ -n "${TARGET}" ]] && grep -q '\.float()\.numpy()' "${TARGET}"; then
  echo "[entrypoint-amd] patching AMD audio SaveVideo bug in ${TARGET}"
  sed -i 's/\.float()\.numpy()/.float().cpu().numpy()/g' "${TARGET}"
elif [[ -n "${TARGET}" ]]; then
  echo "[entrypoint-amd] ${TARGET} already patched or uses .cpu() — skipping"
else
  echo "[entrypoint-amd] WARN: video_types.py not found under /workload|/root|/ComfyUI — skipping audio patch"
fi

exec "$@"
