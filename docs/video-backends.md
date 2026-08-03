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

- Fallback when LTX-2.3 timeline is not packaged / not selected.
- FLF2V is **two-pass** (`wan22_flf2v_high` → `wan22_flf2v_low`) to avoid dual-UNET VRAM crashes on ROCm.
- Frame rule: `4n+1` (e.g. 33).
- Storyboard **Animate this beat** / step clips use a capped size (≤832×480) and default **33** frames.
- VRAM: LTX clips in one beat **reuse** loaded models; Wan FLF **unloads** high→low between passes; models unload after each beat finishes.

## LTX-2 (`ltx2`)

- Checkpoint: **`ltx-2-19b-dev-fp8`**.
- Workflows: `ltx2_t2v` / `ltx2_i2v` / `ltx2_flf2v`.
- Frame rule: **`8n+1`** (e.g. 17, 33).
- Storyboard FLF step clips default to **768×448**.

## LTX-2.3 (`ltx23`)

- Checkpoint / transformer: Distilled 1.1 FP8 transformer (timeline) or `ltx-2.3-22b-distilled-fp8` (FLF/I2V/T2V).
- Workflows: `ltx23_t2v` / `ltx23_i2v` / `ltx23_flf2v`.
- **Optional motion path:** **`ltx23_timeline`** (Skill Destiny) — enable via **Settings → Use LTX-2.3 Skill Destiny timeline**. Then beat animation and movie chunks use up to 4 keyframe guides, Prompt Relay segments, Dual Character IC-LoRA, and 2-pass draft→upscale→refine with baked-in audio.
- Extra: **`ltx23_ic_lora`** (Ingredients reference-sheet; separate from Dual Character).
- Frame rule: **`8n+1`**. Timeline preview canvas **768×512**; quality **960×544** (÷32). FLF step clips still **768×448**.
- Host custom nodes: ComfyUI-LTXVideo (`LTXVAddGuideMulti`, `LTXVChunkFeedForward`, AV latent, …), **ComfyUI-PromptRelay** (`PromptRelayEncodeTimeline`), `ResizeImageMaskNode`, VHS Video Helper Suite.

### Skill Destiny timeline (`ltx23_timeline`)

Based on [Skill Destiny LTX 2.3 workflow](https://www.youtube.com/watch?v=aOOMD4J5D50) (import: `import/ltx23_timeline_dual_character.json`).

**Off by default.** When Settings `use_ltx23_timeline` is on, the timeline graph is packaged (`timeline_ready`), and project/job backend is `ltx23`:

1. Storyboard **Animate this beat** packs the beat’s keyframe series (≤4) + dialog/SFX into one timeline render.
2. Movie agent emits `mode=timeline` chunks (one per beat) instead of many FLF pairs.
3. If the setting is off, timeline fails, or the graph is missing → existing FLF2V / Wan fallback.

Prompt practice: each segment = action + camera + spoken lines in quotes (from beat `dialog`); negative bans subtitles/watermarks/distorted sound.

### Dual Character vs Ingredients IC-LoRA

| Id | LoRA | Role |
|----|------|------|
| `ltx23_timeline` | `LTX2.3-IC-LORA-Dual-Character.safetensors` | Multi-character dialogue + multi-keyframe guides (opt-in via Settings) |
| `ltx23_ic_lora` | `ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors` | Reference-sheet inventory (characters/props/location panels) |

### LTX IC-LoRA Ingredients (reference sheet)

For sheet-conditioned clips use **`ltx23_ic_lora`** ([model card](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Ingredients)):

- Control: composite **reference sheet** → repeated to `num_frames` → `LTXVAddGuide` + Ingredients LoRA
- Prompt: `### Reference Sheet Description` … / `### Target Description` …
- Quality bucket: **768×448**, **121** frames, **24** fps. Preview example: **512×288**, `duration_sec=2` @ 24fps → 49 frames.

## Workflow IDs

| Backend | T2V | I2V | FLF2V | Extra |
|---------|-----|-----|-------|-------|
| wan | `wan22_t2v` | `wan22_i2v` | `wan22_flf2v` (two-pass) | |
| ltx2 | `ltx2_t2v` | `ltx2_i2v` | `ltx2_flf2v` | |
| ltx23 | `ltx23_t2v` | `ltx23_i2v` | `ltx23_flf2v` | `ltx23_timeline`, `ltx23_ic_lora` |

Legacy maps `ltx_t2v` / `ltx_i2v` / `ltx_flf2v` alias to the LTX-2 graphs; `ltx_ic_lora` aliases to `ltx23_ic_lora`.

See `GET /api/video-backends` for readiness (`flf2v_ready`, `timeline_ready`, `ic_lora_ready`, `use_ltx23_timeline`, `ltx23_timeline_enabled`) and `comfyui_workflows/README.md` for packaging.
