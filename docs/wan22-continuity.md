# Wan 2.2 continuity (Local Video Studio)

Condensed from the chunked video station brief. **This app owns control**; ComfyUI stays atomic.

## Rules

1. Frame counts are **`4n+1`** (default chunk = **33**).
2. Same continuous shot → `mode=continue` with overlap discard; new scene → `mode=new_shot`.
3. Lock `prompt_base`, size, fps, steps, CFG, sampler across continues; vary only `prompt_delta`.
4. Intermediate handoff uses **PNG frames**, not repeated lossy MP4 encodes.
5. When concatenating, **do not duplicate** the shared boundary frame (drop first `overlap_frames` of each continue chunk).
6. On join failure, regenerate **only that chunk** (optionally raise overlap).

## v1 method

Rolling **last-frame I2V** after chunk 0 T2V/I2V (simple ComfyUI workflows). Overlap default **12**.

## Handoff schema

See agent `Chunk.handoff` JSON: `shot_id`, `mode`, `chunk_index`, `frame_count`, `overlap_frames`, `last_frame`, `prompt_base`, `prompt_delta`, `seed_policy`, sampler settings, `continuity_notes`.
