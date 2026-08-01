# Video backends (Wan / LTX-2 / LTX-2.3)

Local Video Studio can render motion segments with **Wan 2.2**, **LTX-2 (19B)**, or **LTX-2.3**. Stills (Flux), cast sheet, keyframe series, FLF pair planning, and stitch stay shared — only the segment renderer differs.

## Hierarchy

Resolved per chunk:

1. Chunk `handoff.video_backend`
2. `Shot.video_backend` (optional override)
3. `RenderJob.video_backend` (movie default)
4. `Project.video_backend`
5. Settings `default_video_backend`
6. `"wan"`

UI defaults: Settings → Project → Movie job → per-shot override.

Legacy id `"ltx"` normalizes to **`ltx2`**.

## Mixed backends in one movie

Useful for hero vs filler, capability split, A/B retries, or gradual LTX migration.

Prefer a hard cut (`is_new_shot`) when switching Wan ↔ LTX family (or LTX-2 ↔ LTX-2.3). Continuous FLF across a backend boundary is allowed but may look less seamless.

## Wan

- Default working path.
- FLF2V is **two-pass** (`wan22_flf2v_high` → `wan22_flf2v_low`) to avoid dual-UNET VRAM crashes on ROCm.
- Frame rule: `4n+1` (e.g. 33).
- Storyboard **Animate this beat** / step clips use a capped size (≤832×480) and default **17** frames, plus a soft `free_memory` (no unload) between passes/clips. Full `DEFAULT_WIDTH×HEIGHT` FLF often kills ROCm ComfyUI.

## LTX-2 (`ltx2`)

- Checkpoint: **`ltx-2-19b-dev-fp8`**.
- Workflows: `ltx2_t2v` / `ltx2_i2v` / `ltx2_flf2v`.
- Frame rule: **`8n+1`** (e.g. 17, 33).
- Storyboard FLF step clips default to **768×448**.

## LTX-2.3 (`ltx23`)

- Checkpoint: **`ltx-2.3-22b-distilled-fp8`**.
- Workflows: `ltx23_t2v` / `ltx23_i2v` / `ltx23_flf2v`.
- Extra: **`ltx23_ic_lora`** (Ingredients reference-sheet; not movie-agent default).
- Same frame rule and FLF preview size as LTX-2.
- Host needs custom nodes used by the graphs (`ComfyMathExpression`, `ResizeImageMaskNode`) and matching model filenames under `ComfyUI/models/`.

### LTX IC-LoRA Ingredients (reference sheet)

For strong character/prop/location identity on **LTX-2.3**, use workflow id **`ltx23_ic_lora`** ([model card](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Ingredients)):

- Control: composite **reference sheet** → repeated to `num_frames` → `LTXVAddGuide` + Ingredients LoRA
- Prompt: `### Reference Sheet Description` … / `### Target Description` …
- Size/duration: **`width` / `height` / `fps` / `num_frames`** are flexible. `render_ic_lora` also accepts `duration_sec` and snaps frames to `8n+1` (max 121).
- Quality bucket: **768×448**, **121** frames, **24** fps. Preview example: **512×288**, `duration_sec=2` @ 24fps → 49 frames.
- Not yet the default movie renderer — use `render_ic_lora` / Comfy import for sheet-conditioned clips; studio sheet-builder + agent routing TBD

## Workflow IDs

| Backend | T2V | I2V | FLF2V | Extra |
|---------|-----|-----|-------|-------|
| wan | `wan22_t2v` | `wan22_i2v` | `wan22_flf2v` (two-pass) | |
| ltx2 | `ltx2_t2v` | `ltx2_i2v` | `ltx2_flf2v` | |
| ltx23 | `ltx23_t2v` | `ltx23_i2v` | `ltx23_flf2v` | `ltx23_ic_lora` |

Legacy maps `ltx_t2v` / `ltx_i2v` / `ltx_flf2v` alias to the LTX-2 graphs; `ltx_ic_lora` aliases to `ltx23_ic_lora`.

See `GET /api/video-backends` for readiness (`flf2v_ready`, `ic_lora_ready`) and `comfyui_workflows/README.md` for packaging.
