# ComfyUI workflows for Local Video Studio

This folder ships **simple atomic** video graphs (Wan 2.2 working; LTX slots ready). Continuity (chunking, overlap, stitch) is handled by Local Video Studio — not by extender custom nodes.

See also [docs/video-backends.md](../docs/video-backends.md) for Wan vs LTX selection hierarchy.

## Layout

| Path | Purpose |
|------|---------|
| `import/` | **UI-format JSON** — drag onto ComfyUI canvas or *Workflow → Open* |
| `api/` | **API-format JSON** — what the app POSTs to ComfyUI `/prompt` |
| `maps/` | Logical fields → `node_id.input` for parameterization |

## Video backends

| Backend | Role | T2V / I2V / FLF map IDs |
|---------|------|-------------------------|
| **wan** (default) | Proven Wan 2.2 path; FLF is two-pass high→low | `wan22_t2v`, `wan22_i2v`, `wan22_flf2v` |
| **ltx** | LTX 2.3 T2V / I2V / FLF packaged (distilled FP8) | `ltx_t2v`, `ltx_i2v`, `ltx_flf2v` |
| **ltx ic-lora** | Ingredients reference-sheet path (identity lock) | `ltx_ic_lora` |

### LTX (ready)

API graphs under `api/ltx_{t2v,i2v,flf2v}.json` use the working distilled single-pass topology (SaveVideo only). Confirm `GET /api/video-backends` → all three `*_ready: true` for `ltx`.

| ID | Role |
|----|------|
| `ltx_flf2v` | First + last frame (keyframe bridges) |
| `ltx_i2v` | Start image only (same graph; start fed as both guides) |
| `ltx_t2v` | Text only (guides stripped; size from width/height) |
| `ltx_ic_lora` | **IC-LoRA Ingredients** — reference sheet + two-part prompt |

Models (map `model_files`):

```
ComfyUI/models/
  checkpoints/ltx-2.3-22b-distilled-fp8.safetensors
  text_encoders/gemma_3_12B_it_fp4_mixed.safetensors
  loras/ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors   # IC-LoRA only
```

Frame counts for plain LTX T2V/I2V/FLF must be **`8n+1`** (default **33**). On ROCm, avoid `--fp16-vae` with FP8 LTX (can yield black frames).

### LTX IC-LoRA Ingredients (`ltx_ic_lora`)

Comfy-Org template packaged as `import/ltx_ic_lora.json` + `api/ltx_ic_lora.json`. This is **not** a drop-in for FLF bridges — it expects:

1. A **reference sheet** image (character close-ups + turnaround, props, location; black background, no text)
2. Prompt labeled `### Reference Sheet Description` / `### Target Description` (or `Reference sheet:` / `Generated video:`)
3. Trained bucket: **768×448**, **121 frames**, **24 fps** (sheet is `RepeatImageBatch`’d to `num_frames`)
4. LoRA `ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors` via `GetICLoRAParameters` → `LTXVAddGuide`

`bypass_first_frame` defaults **on** in the API graph (matches the subgraph UI). Studio movie/agent does not auto-route here yet — use the workflow map / Comfy import for sheet-conditioned clips; cast-sheet builder integration is next.

Official Comfy-Org UI blueprints (optional inspect / customize) live in `import/ltx_*.json`.

## Import into ComfyUI

1. Open ComfyUI (`http://192.168.10.31:8188`).
2. Drag one of:
   - `import/wan22_t2v.json` — official 14B T2V template (subgraph; inspect / customize)
   - `import/wan22_i2v.json` — official 14B I2V template
   - `import/wan22_t2v_5b.json` / `wan22_i2v_5b.json` — flat **5B TI2V** graphs tuned to **33 frames** (recommended for the agent)
   - `import/wan22_flf2v.json` — official **14B FLF2V** template (first + last frame)
   - `import/ltx_flf2v.json` / `ltx_i2v.json` / `ltx_t2v.json` — LTX 2.3 UI blueprints (optional inspect)
   - `import/ltx_ic_lora.json` — LTX 2.3 **IC-LoRA Ingredients** reference-sheet template
   - `import/still_hero.json` — simple SD1.5-style still (change checkpoint to yours)
3. Confirm model filenames match your `ComfyUI/models/` tree.
4. Run a test prompt once.

### Models (5B TI2V agent defaults)

```
ComfyUI/models/
  diffusion_models/wan2.2_ti2v_5B_fp16.safetensors
  text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors
  vae/wan2.2_vae.safetensors
```

From [Comfy-Org/Wan_2.2_ComfyUI_Repackaged](https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged).

14B templates need the high/low noise UNET pair + `wan_2.1_vae` — see notes inside those import graphs.

## Sync API graphs after editing

If you change an `import/` graph in ComfyUI:

1. *Workflow → Export (API)* (enable Dev mode if needed).
2. Save over the matching file in `api/` (e.g. `api/wan22_t2v.json`).
3. Update `maps/*.yaml` if node IDs changed.

Agent defaults expect `length` / `num_frames` = **33** (`4n+1`).

Video API graphs (`wan22_t2v`, `wan22_i2v`, `wan22_flf2v`) use **VAE Decode (Tiled)** (`VAEDecodeTiled`) to reduce VRAM during decode.

### FLF2V two-pass (AMD / ROCm)

Wan 2.2’s high→low noise handoff often crashes ComfyUI when both 14B UNETs load in one graph. The app runs FLF as:

1. `wan22_flf2v_high` — high-noise sampler → `SaveLatent`
2. short pause (separate prompt; avoid `POST /free` on ROCm — it can kill the server)
3. `wan22_flf2v_low` — `LoadLatent` + low-noise sampler → tiled decode → video

UNET loaders use `weight_dtype: default` (explicit `fp8_e4m3fn` was less stable here).

## Agent usage

| Profile | When used |
|---------|-----------|
| `wan22_t2v` | Chunk 0 / `new_shot` (Wan) |
| `wan22_i2v` | `continue` — uploads previous `last_frame.png` into LoadImage |
| `wan22_flf2v` | **Keyframe / beat bridges** — two-pass 14B FLF2V (`start_image` + `end_image`); unloads between high/low UNETs |
| `ltx_t2v` | Chunk 0 / `new_shot` when backend is `ltx` |
| `ltx_i2v` | `continue` when backend is `ltx` (start image → both guides) |
| `ltx_flf2v` | **Keyframe / beat bridges** when backend is `ltx` |
| `ltx_ic_lora` | Reference-sheet Ingredients clips (not auto-routed by movie agent yet) |
| `still_hero` | Storyboard stills (text → image) |
| `still_edit` | Prompt-edit an existing still (ReferenceLatent) |

No Wan Video Extender / chunk helper nodes required.
