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

## LTX

- Same interface (`render_flf2v` / `render_i2v` / `render_t2v`).
- Expectation: **single-graph** FLF when graphs exist (`ltx_flf2v`).
- Until `comfyui_workflows/api/ltx_*.json` are exported, LTX raises a clear `WorkflowError` pointing at the README — no fake opaque graphs.

## Workflow IDs

| Backend | T2V | I2V | FLF2V |
|---------|-----|-----|-------|
| wan | `wan22_t2v` | `wan22_i2v` | `wan22_flf2v` (two-pass) |
| ltx | `ltx_t2v` | `ltx_i2v` | `ltx_flf2v` |

See `GET /api/video-backends` for readiness (`flf2v_ready`) and `comfyui_workflows/README.md` for packaging.
