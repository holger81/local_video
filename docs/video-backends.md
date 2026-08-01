# Video backends (Wan / LTX)

Local Video Studio can render motion segments with either **Wan 2.2** or **LTX**. Stills (Flux), cast sheet, keyframe series, FLF pair planning, and stitch stay shared — only the segment renderer differs.

## Hierarchy

Resolved per chunk:

1. Chunk `handoff.video_backend`
2. `Shot.video_backend` (optional override)
3. `RenderJob.video_backend` (movie default)
4. `Project.video_backend`
5. Settings `default_video_backend`
6. `"wan"`

UI defaults: Settings → Project → Movie job → per-shot override.

## Mixed backends in one movie

Useful for hero vs filler, capability split, A/B retries, or gradual LTX migration.

Prefer a hard cut (`is_new_shot`) when switching Wan ↔ LTX. Continuous FLF across a backend boundary is allowed but may look less seamless (style/motion mismatch; join QA may be looser).

## Wan

- Default working path.
- FLF2V is **two-pass** (`wan22_flf2v_high` → `wan22_flf2v_low`) to avoid dual-UNET VRAM crashes on ROCm.
- Frame rule: `4n+1` (e.g. 33).
- Storyboard **Animate this beat** / step clips use a capped size (≤832×480) and default **17** frames, plus a soft `free_memory` (no unload) between passes/clips. Full `DEFAULT_WIDTH×HEIGHT` FLF often kills ROCm ComfyUI.

## LTX

- Same interface (`render_flf2v` / `render_i2v` / `render_t2v`).
- **T2V / I2V / FLF are shipped** as distilled single-pass graphs (`api/ltx_*.json` + maps).
- Frame rule: **`8n+1`** (e.g. 33). Planning snaps to this when the job/shot backend is `ltx`.
- Storyboard FLF step clips default to the quality bucket **768×448** (not full project defaults).
- I2V reuses the FLF topology with the start image as both guides; T2V drops image guides and sizes the latent from width/height.
- Host needs custom nodes used by the graphs (`ComfyMathExpression`, `ResizeImageMaskNode`) and matching model filenames under `ComfyUI/models/`.

### LTX IC-LoRA Ingredients (reference sheet)

For strong character/prop/location identity, use workflow id **`ltx_ic_lora`** ([model card](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Ingredients)):

- Control: composite **reference sheet** → repeated to `num_frames` → `LTXVAddGuide` + Ingredients LoRA
- Prompt: `### Reference Sheet Description` … / `### Target Description` …
- Size/duration: **`width` / `height` / `fps` / `num_frames`** are flexible. `LtxBackend.render_ic_lora` also accepts `duration_sec` and snaps frames to `8n+1` (max 121).
- Quality bucket: **768×448**, **121** frames, **24** fps. Preview example: **512×288**, `duration_sec=2` @ 24fps → 49 frames.
- Not yet the default movie renderer — use `render_ic_lora` / Comfy import for sheet-conditioned clips; studio sheet-builder + agent routing TBD

## Workflow IDs

| Backend | T2V | I2V | FLF2V | Extra |
|---------|-----|-----|-------|-------|
| wan | `wan22_t2v` | `wan22_i2v` | `wan22_flf2v` (two-pass) | |
| ltx | `ltx_t2v` | `ltx_i2v` | `ltx_flf2v` | `ltx_ic_lora` (sheet; flexible W/H/duration) |

See `GET /api/video-backends` for readiness (`flf2v_ready`, `ic_lora_ready`) and `comfyui_workflows/README.md` for packaging.
