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

## Redeploy

These fixes need an image rebuild + Portainer redeploy to reach the LAN host.
