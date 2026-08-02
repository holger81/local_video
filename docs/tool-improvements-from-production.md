# Local Video Studio — improvements from real project use

Notes from building **Little Astronauts – S01E01** (~10 min board) against the live studio API + ComfyUI + llama.cpp. Ordered by impact.

---

## 1. Storyboard lifecycle (highest impact)

### Don’t wipe frames before propose succeeds
`propose_storyboard` currently deletes all frames, then calls the LLM. If JSON parse fails or the model returns garbage, the board is empty and prior stills/keyframes are gone.

**Change:** generate the proposed list first; only replace frames after a non-empty, validated result. Prefer a transaction: insert new → delete old, or write to a staging table.

### Add explicit frame CRUD
There is no `POST /frames` (create/append). Agents and humans are forced to:

1. Temporarily overwrite the story with a beat outline  
2. Call `propose` (which wipes)  
3. Restore the real story  
4. PATCH every frame  

**Change:**

- `POST /projects/{id}/storyboard/frames` — create one frame (or bulk)  
- `POST /projects/{id}/storyboard/replace` — replace board from a JSON array **without** LLM  
- Optional: `DELETE` / reorder frames  

This is the single biggest unlock for “dense episode” workflows and agent-driven boards.

### Target runtime, not just max_frames
`max_frames` alone is a weak knob. A 10‑minute kids episode needs ~30–40 beats at ~12–20s, not 8×4s.

**Change:** accept something like:

```json
{ "target_duration_sec": 600, "max_frames": 40, "avg_beat_sec": 15 }
```

Validate after propose: warn if `sum(duration_hint_sec)` is far from target. UI: show total runtime next to the board.

### Propose must honor exact count
Small local models often return fewer frames than requested (e.g. asked 37, got 31–33).

**Change:**

- Retry / continue-generation until count is met, or  
- Pad with placeholder beats derived from a beat outline, or  
- Accept a client-supplied beat list and skip the LLM entirely (`replace` above)

---

## 2. LLM integration (llama.cpp / small models)

### Long stories break propose / cast extract
Full episode prose (~14k chars) overwhelms LFM2-class models: bad JSON, truncated arcs, missing cast (e.g. Max/Sam never auto-detected).

**Change:**

- For propose/cast: send a **structured synopsis or beat list**, not the full novella  
- Keep full story for dialog/audio context only (or RAG-style excerpts)  
- Harden `_extract_json` further (repair trailing commas, take first JSON array only, reject “extra data”)  
- Surface the raw model reply in API errors (already partly done for cast)

### Cast detect should merge, not under-create
`detect_characters` with `replace_auto=false` still depends on the LLM listing everyone. Named recurring cast in the story was missed.

**Change:**

- Heuristic pass: proper names + pronouns / “X and Y” patterns before/after LLM  
- UI “Add from story highlights”  
- Never downgrade approved characters; fill gaps only  

### Appearance / Applied edits pollution
Outfit edit instructions get appended into `appearance_prompt` as “Applied edits: …”, which then fights wardrobe locks (spacesuit text on summer looks).

**Change:**

- Store edit history separately (or on the outfit only)  
- Keep `appearance_prompt` as face/body identity  
- Outfit `prompt` = clothing only; strip Applied-edits clutter on save  

(Related wardrobe/face-body lock work in `llm.py` helped stills; the data model should match.)

---

## 3. Outfits and references

### Summer / default looks must not inherit helmets
Character base refs and summer outfit refs repeatedly regenerated with bubble helmets because the project premise is “astronauts” and base refs were EVA suits.

**Change:**

- When generating a **non-spacesuit** outfit or default summer portrait: strong negative prompt + instruction template (“no helmet, no neck ring, no glass dome”)  
- Default character reference should follow the **default outfit**, not the spacesuit  
- Batch “audit outfits” tool: flag helmet/spacesuit tokens on outfits whose name/prompt says summer/play/everyday  

### Library upload + set-from-media on the deployed API
Local code has `/library/upload` and `…/reference/from-media`, but the deployed OpenAPI often lacks them. Agents then cannot push a user-supplied Max portrait into the cast without edit-from-existing.

**Change:** ship and document those endpoints; UI “Use this image as reference” on cast/outfit.

### First outfit generate vs edit
`generate_outfit_reference` with `instruction` but no existing outfit still returns *“generate an outfit look before applying an edit”*. Easy to misuse from agents.

**Change:** if no outfit ref exists, treat instruction as part of the **first** generate prompt instead of requiring a two-step dance.

---

## 4. Dialog / audio

### One field is fine; two modes are not
`dialog` is both spoken lines and the audio cue sheet (SFX/Music). Curated dialog is good; LLM `plan_beat_audio_prompt` can overwrite it with weaker small-model lines.

**Change:**

- Split or namespace: `dialog` (speech) + `audio_notes` (SFX/music), concatenated at render time  
- “Generate audio” should **enrich** existing dialog (add SFX block) rather than replace speech  
- Batch endpoint: `POST …/storyboard/dialogs` for all beats  

### Runtime-aware dialog length
A 30s beat with two short lines feels empty; a 12s beat with a paragraph won’t fit.

**Change:** pass `duration_hint_sec` into dialog generation with guidance (“~N seconds of speech”).

---

## 5. Keyframes and long ComfyUI runs

### Batch keyframes need a job, not a single HTTP request
Per-slot generation is ~2–6 minutes (cast-lock first frames worse). A 10‑minute board can mean 100+ slots → multi-hour. Client HTTP timeouts and “remote end closed connection” are normal.

**Change:**

- ARQ (or similar) job: `generate_all_keyframes` with progress events, pause/resume/cancel (same pattern as movie agent)  
- Persist progress per frame/slot; `skip_existing` already helps — make resume first-class in UI  
- Don’t block the API worker on one giant request  

### Rebuild prompts before generate is good — keep path preservation
`rebuild_frame_keyframe_prompts` preserving existing paths by index is correct. Document that changing duration/slots can drop path alignment.

### Continuous beats + shared first frame
Sharing previous last keyframe as next first is the right continuity model. Failures mid-chain leave later continuous beats broken.

**Change:** job graph should regenerate forward from the first missing slot in a continuous run; UI badge “needs previous beat’s last keyframe”.

---

## 6. Agent / MCP ergonomics

### Tool surface for episode assembly
Useful MCP/REST tools that were missing or awkward in practice:

| Tool | Why |
|------|-----|
| `replace_storyboard(frames[])` | Curated 10‑min boards without LLM |
| `list_frames` / get single frame | Inspect without full project dump |
| `set_frame_cast` | Already via PATCH — fine |
| `batch_generate_dialogs` | Audio pass |
| `start_keyframes_job` / `get_keyframes_job` | Long renders |
| `upload_library_image` + `set_character_reference_from_media` | User photos as cast |

### Don’t require story overwrite to propose structure
The outline→propose→restore dance is a footgun (easy to leave the outline as the story). Replace-board API removes it.

---

## 7. UI / product

- Show **total runtime** (`Σ duration_hint_sec`) on the storyboard header; warn if &lt; target.  
- “Expand to N minutes” action: densify beats or scale durations with confirmation.  
- Outfit gallery with helmet/spacesuit warnings.  
- Cast completeness checklist vs names mentioned in story.  
- Keyframe job progress bar + cancel (match movie agent).  

---

## 8. Ops / deploy

- Local fixes (e.g. propose delete-ordering, JSON hardening) only help after **image rebuild + Portainer redeploy**. Document that agents editing the repo do not hot-patch `192.168.10.31`.  
- ComfyUI connection drops during long still/keyframe edits: retries with backoff in the client already help; add them at the service layer for all Comfy waits.  

---

## 9. Suggested implementation order

1. **Safe propose** (no wipe on failure) + **`replace_storyboard` API**  
2. **Keyframe generation as ARQ job** with resume  
3. **Dialog vs audio_notes** split + enrich-only generate  
4. **Outfit/default-ref policy** (no helmet on summer) + library from-media on deploy  
5. **Runtime-aware propose** (`target_duration_sec`) + cast detect heuristics  

### Implemented (code)

- Safe propose + `POST …/replace`, `POST/GET/DELETE …/frames`, runtime knobs  
- `POST …/keyframes/job` + ARQ `run_keyframes_job` (pause/resume/cancel via existing job APIs)  
- `audio_notes` column; dialog generate enrich-only; `POST …/dialogs` batch  
- Helmet-free negatives for casual outfits; default portrait follows default outfit; first outfit generate accepts `instruction`; `GET …/outfit-audit`  
- Cast detect heuristic name pass + story excerpt for propose/cast; JSON repair in `_extract_json`  
- Library upload / from-media already shipped earlier — **redeploy** the image for the LAN host  

---

## 10. What already worked well

- Cast sheet + iterative cast lock for multi-character stills/keyframes  
- Outfit wardrobe vs face/body separation in prompts (when data is clean)  
- Continuous-shot keyframe sharing  
- Story approve → cast detect hook (when the model lists people)  
- Per-frame PATCH for description / visual / dialog / cast / duration  
- Skipping existing keyframe paths for resume  

These should stay; most pain was **board mutation**, **small-LLM propose**, and **long-running keyframe orchestration**.
