# Wan 2.2 continuity (Local Video Studio)

For choosing Wan vs LTX (and mixing backends in one movie), see [video-backends.md](video-backends.md).

Condensed from the chunked video station brief. **This app owns control**; ComfyUI stays atomic.

## Rules

1. Frame counts are **`4n+1`** (default chunk = **33**).
2. Same continuous shot → `mode=continue` with overlap discard; new scene → `mode=new_shot`.
3. Lock `prompt_base`, size, fps, steps, CFG, sampler across continues; vary only `prompt_delta`.
4. Intermediate handoff uses **PNG frames**, not repeated lossy MP4 encodes.
5. When concatenating, **do not duplicate** the shared boundary frame (drop first `overlap_frames` of each continue chunk).
6. On join failure, regenerate **only that chunk** (optionally raise overlap).

## v1 method (fallback)

Rolling **last-frame I2V** after chunk 0 T2V/I2V when a shot has **no** ready keyframe
series. Overlap default **12**. Chunk 0 may I2V-lock the first keyframe/still.

## Keyframe-driven movie (default when keyframes exist)

When a shot’s storyboard beat has a complete keyframe series (paths set, ≥2 frames),
the movie agent plans **one FLF2V chunk per consecutive keyframe pair** (same method as
storyboard step clips). Continuous beats (`is_new_shot=false`) **share** the previous
beat’s last keyframe as this beat’s first (exact same path/prompt) — no re-render, and
**no** inter-beat FLF bridge when the boundary is shared. Motion continues through the
next beat’s internal keyframe pairs.

- `mode=flf2v` with `start_image_path` + `end_image_path`
- Frame count from keyframe `t_sec` Δ (snapped to `4n+1`)
- Transition prompts from per-keyframe `image_prompt`s
- Adjacent FLF segments (and storyboard step-clip concats) drop 1 shared boundary frame

Storyboard pipeline:
1. **Characters** — cast ground truth (auto-detected from story; appearance prompts + refs)
2. Hero stills per beat (cast sheet injected)
3. **Keyframes** — variable series per beat; continues share prior end as first
4. **Step clips** — FLF2V between consecutive keyframes (concat with boundary subtract)
5. **Between steps** — FLF2V only across true discontinuities (new shots)
6. **Movie agent** — same keyframe pairs / shared-boundary rules

True dual-frame FLF uses the `wan22_flf2v` workflow (Wan 2.2 14B first+last frame),
run as **two ComfyUI prompts** (high-noise → unload → low-noise) so both 14B UNETs are
never resident together. TI2V 5B I2V (`wan22_i2v`) only locks the start frame.

## Handoff schema

See agent `Chunk.handoff` JSON: `shot_id`, `mode` (`new_shot` | `continue` | `flf2v`),
`chunk_index`, `frame_count`, `overlap_frames`, `last_frame`, `prompt_base`,
`prompt_delta`, `seed_policy`, sampler settings, `continuity_notes`.
For `flf2v`: also `start_image_path`, `end_image_path`, and optional `frame_id`.
