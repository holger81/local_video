# Keyframe review interventions — Little Astronauts S01E01

Track every manual correction during beat-by-beat QA so we can harden the tool later.

| Beat | Issue | Action taken | Tool fix idea | Status |
|------|-------|--------------|---------------|--------|
| 0 (id=9) establishing | LLM keyframe prompts invented Aunt Dani close-ups despite empty cast / “no people” beat | Manually replaced with 3 landscape-only prompts | `plan_keyframe_series` must respect empty cast + beat text; forbid inventing characters not in cast | **Fixed** — empty cast passes `empty_cast=True`; planner forbids inventing people |
| 0 | Keyframe generate with `cast=[]` still ran cast-lock and inserted full project cast (crowd on farmyard) | Regenerating via normal keyframe API unusable for empty beats | Empty frame cast must mean **no** cast sheet / no cast-lock path (do not fall back to full project cast) | **Fixed** — `cast=[]` → no panels / empty sheet; no full-project fallback |
| 0 | `still_edit` “remove all people” ignored — cast remained | Abandoned edit path | Stronger negatives + empty-scene mode; or skip cast-conditioned workflows for no-cast beats | **Fixed** — empty cast / remove-people edits clear cast sheet + empty-scene negatives |
| 0 | `POST …/keyframes/from-media` hits `{phase}` route → `"phase must be first, mid, last…"` | Workaround: PATCH keyframe `path`s to freeform `/images/generate` outputs | Register `/keyframes/from-media` **before** `/keyframes/{phase}` | **Fixed** — static route registered before `{phase}` |
| 0 | Final fix: freeform `still_hero` gens (no people) assigned as kf0–kf2 | Beat 0 now uses empty farmhouse/sunflowers/barn keyframes | Prefer freeform/T2I when cast empty; document agent workaround | **Fixed** — empty cast uses `still_hero` + empty-scene prompt/negatives |

## Semantics (post-fix)

- **`cast=[]`** on a beat means **no people**: no cast sheet, no cast-lock panels, T2I/`still_hero` with empty-scene negatives.
- **Continuous beats** with empty cast still **inherit** the previous beat’s cast when generating a hero still (unchanged).
- Beats that need characters must list them in `frame.cast` (UI cast picker / PATCH). Prompt-only naming is not enough when cast is empty.
- Agents can still assign library/freeform images via `POST …/keyframes/from-media`.

## Follow-up (identity lock)

Beat 1 (Em+Jo porch) still drifted off cast refs after the empty-cast fix: Em’s blue eyes / teal leaf dress and Jo’s white sneakers did not survive pass-0 restage-from-portrait, and mid `still_edit` never re-applied cast refs.

**Hardening (code):**
- Fresh casted keyframes/stills: people-free environment plate → INSERT each cast panel
- Mid/last continuity: still_edit pose/camera, then identity-refresh rewrite from cast refs
- Empty-scene negatives only when `cast=[]` (not when name-filter yields no panels)
- Stronger keep-cast + eye/skin/wardrobe identity language
- **Cast lock now uses `still_edit_dual`** (Flux.2 Klein 9B, image 1 = scene, image 2 = cast, 4 steps; positives end with “Do not change anything else in the image.”)

## Redeploy

These fixes need an image rebuild + Portainer redeploy to reach the LAN host.

---

## Regression tracker (post tool-fix deploy)

Check each previously fixed / claimed-fixed issue when reviewing later beats.

| Check | Beat 0 | Beat 1 (id=10) | Beat 2+ |
|-------|--------|----------------|---------|
| Planner invents off-cast characters | OK after fix | **OK** — prompts name only Em+Jo | |
| `cast=[]` still cast-locks full project cast | **OK** | n/a (cast set) | |
| `still_edit` remove-people ignored | **OK** (used freeform) | n/a | |
| `keyframes/from-media` shadowed by `{phase}` | **OK** (probe) | **OK** | |
| Empty-cast uses T2I / no people | **OK** | n/a | |
| Cast-lock limited to frame cast (not full project) | n/a | **OK** — only Em+Jo | |

## Beat 1 — Em+Jo porch (id=10) — still open identity issues

| Beat | Issue | Action taken | Tool fix idea | Status |
|------|-------|--------------|---------------|--------|
| 1 | **9 keyframes** for ~15.5s beat (dense / expensive) | Left as-is for now | `keyframe_plan_times` spaces ≤2s → ~D/2+1 slots; for 10‑min boards use ~1 per 5–6s (min 3) | Open — root cause confirmed |
| 1 | Em **blue eyes** (clear on outfit ref) → brown/dark in nearly all slots | Edit instructions explicitly asked for blue eyes on kf 0,1,3,5,8 | Identity-refresh must re-apply eye color from cast refs; appearance_prompt should state blue eyes (currently omits) | **Still occurs** |
| 1 | Jo hair drifts to buzz cut; shorts/sneakers often grey/black not white; landscape shirt → plain blue | Same edit pass | Stronger wardrobe lock from outfit ref on mid/last continuity | **Still occurs** (partial: shirt ok on some slots) |
| 1 | Bags missing early; later two **identical** duplicate bags | Edits added bags; duplicates remain | Prop consistency: one bag per kid, distinct looks; seed props from first frame | **Still occurs** |
| 1 | `still_edit` on kf0 produced **triplicate Em** (+ sandals vs barefoot) | Regenerating `keyframes/first` | Edit must hard-cap cast count; reject multi-instance of same character | **Still occurs** (edit regression) |
| 1 | Em appearance_prompt lacks eye color; Jo outfit prompt says “sneakers” not “white” | Logged; not patched mid-review | Sync appearance/outfit text with approved refs | Open |
