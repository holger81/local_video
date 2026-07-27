# ComfyUI workflows for Local Video Studio

This folder ships **simple atomic** Wan 2.2 graphs. Continuity (chunking, overlap, stitch) is handled by Local Video Studio — not by extender custom nodes.

## Layout

| Path | Purpose |
|------|---------|
| `import/` | **UI-format JSON** — drag onto ComfyUI canvas or *Workflow → Open* |
| `api/` | **API-format JSON** — what the app POSTs to ComfyUI `/prompt` |
| `maps/` | Logical fields → `node_id.input` for parameterization |

## Import into ComfyUI

1. Open ComfyUI (`http://192.168.10.31:8188`).
2. Drag one of:
   - `import/wan22_t2v.json` — official 14B T2V template (subgraph; inspect / customize)
   - `import/wan22_i2v.json` — official 14B I2V template
   - `import/wan22_t2v_5b.json` / `wan22_i2v_5b.json` — flat **5B TI2V** graphs tuned to **33 frames** (recommended for the agent)
   - `import/wan22_flf2v.json` — official **14B FLF2V** template (first + last frame)
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

## Agent usage

| Profile | When used |
|---------|-----------|
| `wan22_t2v` | Chunk 0 / `new_shot` |
| `wan22_i2v` | `continue` — uploads previous `last_frame.png` into LoadImage |
| `wan22_flf2v` | **Keyframe / beat bridges** — uploads `start_image` + `end_image` (14B FLF2V) |
| `still_hero` | Storyboard stills (text → image) |
| `still_edit` | Prompt-edit an existing still (ReferenceLatent) |

No Wan Video Extender / chunk helper nodes required.
