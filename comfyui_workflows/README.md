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
| **ltx2** | LTX-2 19B FP8 | `ltx2_t2v`, `ltx2_i2v`, `ltx2_flf2v` |
| **ltx23** | LTX-2.3 22B distilled FP8 | `ltx23_t2v`, `ltx23_i2v`, `ltx23_flf2v` (+ `ltx23_ic_lora`) |

Legacy `"ltx"` / maps `ltx_*` alias to **ltx2** motion graphs (IC-LoRA legacy → ltx23).

### LTX-2 / LTX-2.3

Confirm `GET /api/video-backends` → `flf2v_ready` for `ltx2` and `ltx23`.

| ID | Role |
|----|------|
| `ltx2_flf2v` / `ltx23_flf2v` | First + last frame (keyframe bridges) |
| `ltx2_i2v` / `ltx23_i2v` | Start image only (start fed as both guides) |
| `ltx2_t2v` / `ltx23_t2v` | Text only |
| `ltx23_ic_lora` | **IC-LoRA Ingredients** — reference sheet + two-part prompt |

Models (map `model_files`):

```
ComfyUI/models/
  checkpoints/ltx-2-19b-dev-fp8.safetensors            # ltx2
  checkpoints/ltx-2.3-22b-distilled-fp8.safetensors    # ltx23 (+ IC-LoRA)
  text_encoders/gemma_3_12B_it_fp4_mixed.safetensors
  loras/ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors   # ltx23 IC-LoRA only
```

Frame counts for LTX T2V/I2V/FLF must be **`8n+1`** (default **33**). On ROCm, avoid `--fp16-vae` with FP8 LTX (can yield black frames).

### LTX IC-LoRA Ingredients (`ltx23_ic_lora`)

Packaged as `import/ltx23_ic_lora.json` + `api/ltx23_ic_lora.json`. Not a drop-in for FLF bridges — expects:

1. A **reference sheet** image (character close-ups + turnaround, props, location; black background, no text)
2. Prompt labeled `### Reference Sheet Description` / `### Target Description`
3. Parameterized **`width` / `height` / `fps` / `num_frames`** (or `duration_sec` via `render_ic_lora`, snapped to `8n+1`)
4. LoRA `ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors`

Trained bucket: **768×448**, **121** frames, **24** fps. Studio movie/agent does not auto-route here yet.

## Import into ComfyUI

1. Open ComfyUI (`http://192.168.10.31:8188`).
2. Drag one of:
   - `import/wan22_t2v.json` — official 14B T2V template (subgraph; inspect / customize)
   - `import/wan22_i2v.json` — official 14B I2V template
   - `import/wan22_t2v_5b.json` / `wan22_i2v_5b.json` — flat **5B TI2V** graphs tuned to **33 frames** (recommended for the agent)
   - `import/wan22_flf2v.json` — official **14B FLF2V** template (first + last frame)
   - `import/ltx2_*.json` — LTX-2 (19B) UI blueprints
   - `import/ltx23_*.json` — LTX-2.3 UI blueprints (+ `ltx23_ic_lora`)
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

Video API graphs use **VAE Decode (Tiled)** (`VAEDecodeTiled`) to reduce VRAM during decode. Defaults in `api/` are `tile_size=512`, `overlap=128`, `temporal_size=16`, `temporal_overlap=8`. On queue the backend reclamps: spatial stays 512/128; temporal grows to 24 on ≥33-frame clips but always stays **strictly below** `num_frames` (values ≥ clip length decode in one temporal chunk and crash ROCm). The older 256/32/8/4 clamp left visible grid seams and temporal ghosting on step previews.

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
| `ltx2_t2v` / `ltx23_t2v` | Chunk 0 / `new_shot` when backend is `ltx2` / `ltx23` |
| `ltx2_i2v` / `ltx23_i2v` | `continue` (start image → both guides) |
| `ltx2_flf2v` / `ltx23_flf2v` | **Keyframe / beat bridges** |
| `ltx23_ic_lora` | Reference-sheet Ingredients clips (not auto-routed yet) |
| `still_hero` | Storyboard stills (text → image) |
| `still_edit` | Prompt-edit an existing still (single ReferenceLatent) |
| `still_edit_dual` | **Cast lock** — dual ReferenceLatent (image 1 = scene, image 2 = cast); Flux.2 Klein 9B; positives end with “Do not change anything else in the image.” |

No Wan Video Extender / chunk helper nodes required.
