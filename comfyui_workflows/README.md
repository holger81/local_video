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
| **ltx23** | LTX-2.3 22B distilled | `ltx23_t2v`, `ltx23_i2v`, `ltx23_flf2v`, `ltx23_timeline` (opt-in), `ltx23_ic_lora` |

Legacy `"ltx"` / maps `ltx_*` alias to **ltx2** motion graphs (IC-LoRA legacy → ltx23).

### LTX-2 / LTX-2.3

Confirm `GET /api/video-backends` → `flf2v_ready` for `ltx2` and `ltx23`.

| ID | Role |
|----|------|
| `ltx2_flf2v` / `ltx23_flf2v` | First + last frame (keyframe bridges) |
| `ltx2_i2v` / `ltx23_i2v` | Start image only (start fed as both guides) |
| `ltx2_t2v` / `ltx23_t2v` | Text only |
| `ltx23_timeline` | **Opt-in (Settings)** — Skill Destiny 4-guide timeline + Dual Character + AV 2-pass |
| `ltx23_ic_lora` | IC-LoRA Ingredients — reference sheet + two-part prompt |

Models (map `model_files`):

```
ComfyUI/models/
  checkpoints/ltx-2-19b-dev-fp8.safetensors            # ltx2
  checkpoints/ltx-2.3-22b-distilled-fp8.safetensors    # ltx23 FLF/I2V/T2V (+ Ingredients)
  diffusion_models/ltx-2.3-22b-distilled-1.1_transformer_only_fp8_scaled.safetensors  # timeline
  text_encoders/gemma_3_12B_it_fp4_mixed.safetensors
  text_encoders/gemma_3_12B_it_fp8_e4m3fn.safetensors
  text_encoders/ltx-2.3_text_projection_bf16.safetensors
  vae/LTX23_video_vae_bf16.safetensors
  vae/LTX23_audio_vae_bf16.safetensors
  vae/taeltx2_3.safetensors
  latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors
  loras/LTX2.3-IC-LORA-Dual-Character.safetensors      # timeline
  loras/ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors   # Ingredients only
```

Frame counts for LTX T2V/I2V/FLF/timeline must be **`8n+1`** (default **33** / timeline sum of segments). On ROCm, avoid `--fp16-vae` with FP8 LTX (can yield black frames).

### LTX-2.3 Skill Destiny timeline (`ltx23_timeline`)

- UI import: `import/ltx23_timeline_dual_character.json`
- API/map: `api/ltx23_timeline.json` + `maps/ltx23_timeline.yaml`
- Custom nodes: **ComfyUI-PromptRelay**, ComfyUI-LTXVideo (`LTXVAddGuideMulti`, chunk FF, AV), VHS, ResizeImageMaskNode
- Studio packs ≤4 keyframes + dialog into `local_prompts` / `segment_lengths` when Settings **`use_ltx23_timeline`** is enabled and backend is `ltx23`

### LTX IC-LoRA Ingredients (`ltx23_ic_lora`)

Packaged as `import/ltx23_ic_lora.json` + `api/ltx23_ic_lora.json`. Not a drop-in for FLF bridges — expects:

1. A **reference sheet** image (character close-ups + turnaround, props, location; black background, no text)
2. Prompt labeled `### Reference Sheet Description` / `### Target Description`
3. Parameterized **`width` / `height` / `fps` / `num_frames`** (or `duration_sec` via `render_ic_lora`, snapped to `8n+1`)
4. LoRA `ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors`

Trained bucket: **768×448**, **121** frames, **24** fps.

## Import into ComfyUI

1. Open ComfyUI (`http://192.168.10.31:8188`).
2. Drag one of:
   - `import/wan22_t2v.json` — official 14B T2V template (subgraph; inspect / customize)
   - `import/wan22_i2v.json` — official 14B I2V template
   - `import/wan22_t2v_5b.json` / `wan22_i2v_5b.json` — flat **5B TI2V** graphs tuned to **33 frames** (recommended for the agent)
   - `import/wan22_flf2v.json` — official **14B FLF2V** template (first + last frame)
   - `import/ltx2_*.json` — LTX-2 (19B) UI blueprints
   - `import/ltx23_*.json` — LTX-2.3 UI blueprints (+ `ltx23_ic_lora`)
   - `import/ltx23_timeline_dual_character.json` — Skill Destiny timeline (Dual Character + Prompt Relay)
   - `import/still_hero.json` — simple SD1.5-style still (change checkpoint to yours)
   - `import/flux2_dev_edit_fp8_dual.json` — Flux.2 Dev dual-ref product-mockup style (enable UNET FP8)
   - `import/flux2_dev_edit_gguf.json` — Flux.2 Dev edit with UnetLoaderGGUF
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
| `still_edit` | Prompt-edit an existing still (single ReferenceLatent; Flux.2 Klein 9B) |
| `still_edit_dual` | **Cast / scenery lock (default)** — Flux.2 Dev FP8 dual ReferenceLatent (image 1 = scene, image 2 = cast/location); `UNETLoader` + `flux2_dev_fp8mixed` |
| `still_edit_flux2_multi` | Multi-ref cast lock (scene + up to 3 cast refs in one pass) |
| `still_edit_flux2_gguf` | Same dual-ref topology with `UnetLoaderGGUF` + `flux2-dev-Q6_K.gguf` |
| `still_edit_dual_klein` | Archived Klein 9B dual-ref (rollback) |

No Wan Video Extender / chunk helper nodes required.

## Flux.2 Dev stills / multi-reference

Cast lock and scenery rewrite default to **Flux.2 Dev FP8** multi-reference (not Klein). Official pattern: chain `LoadImage → ImageScaleToTotalPixels → VAEEncode → ReferenceLatent` (up to 10 refs). See [ComfyUI Flux.2 Dev](https://docs.comfy.org/tutorials/flux/flux-2-dev#multi-image-reference-workflow).

Studio API graphs keep outputs **small**: canvas fixed at **1024×576**, and each reference is scaled to **0.5 MP** (not Flux’s 1–4 MP preview/default).

| Import (UI) | API / map | Loader |
|-------------|-----------|--------|
| `import/flux2_dev_edit_fp8_dual.json` | `still_edit_dual` / `still_edit_flux2` | **`UNETLoader`** `flux2_dev_fp8mixed.safetensors` (do not leave Load Diffusion Model bypassed) |
| `import/flux2_dev_edit_gguf.json` | `still_edit_flux2_gguf` | **`UnetLoaderGGUF`** `flux2-dev-Q6_K.gguf` (requires ComfyUI-GGUF) |

```
ComfyUI/models/
  diffusion_models/flux2_dev_fp8mixed.safetensors
  text_encoders/mistral_3_small_flux2_fp8.safetensors
  vae/flux2-vae.safetensors
  unet/flux2-dev-Q6_K.gguf          # GGUF path only
```

UI import graphs may ship with GGUF active and FP8 UNET bypassed — for FP8, enable the UNET loader and bypass GGUF (or use the studio API graphs, which already use the correct loader).
