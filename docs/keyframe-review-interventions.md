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
- **Scenery** on a beat seeds the environment plate and mid/last scenery dual-ref rewrite when a location ref exists.

## Follow-up (identity lock)

Beat 1 (Em+Jo porch) still drifted off cast refs after the empty-cast fix: Em’s blue eyes / teal leaf dress and Jo’s white sneakers did not survive pass-0 restage-from-portrait, and mid `still_edit` never re-applied cast refs.

**Hardening (code — lock-first keyframes):**
- Fresh casted keyframes/stills: people-free environment plate → INSERT each cast panel
- Mid/last continuity: constrained `still_edit` → identity-refresh REWRITE from cast refs → scenery REWRITE when assigned
- Empty-cast mid/last: inject empty-scene positive into edit path; prefer scenery dual-ref when a location ref is set
- Clear `cast_ref_sheet.png` when `cast=[]`
- Identity bullets from appearance + outfit on refresh; exactly-N count lock language
- Cast lock uses `still_edit_dual` (Flux.2 Klein 9B)
- Keyframe spacing default **~5s** (min 3 slots when duration ≥ ~4s); override `spacing_sec=2` for dense beats

## Redeploy

These fixes need an image rebuild + Portainer redeploy to reach the LAN host. Until then, MCP mid/last regen still runs the pre-lock-first path.

---

## Regression tracker (post tool-fix deploy)

Check each previously fixed / claimed-fixed issue when reviewing later beats.

| Check | Beat 0 | Beat 1 (id=10) | Beat 2+ |
|-------|--------|----------------|---------|
| Planner invents off-cast characters | OK after fix | **OK** — prompts name only Em+Jo | |
| `cast=[]` still cast-locks full project cast | **OK** on first | n/a (cast set) | |
| `still_edit` remove-people ignored | n/a this pass | n/a | |
| `keyframes/from-media` shadowed by `{phase}` | **OK** (used for fix) | **OK** | |
| Empty-cast uses T2I / no people | **Code fixed** — mid/last empty-scene + scenery path; **needs redeploy** to verify | n/a | |
| Cast-lock limited to frame cast (not full project) | n/a | **OK** — only Em+Jo | |
| Empty-cast still has `cast_ref_sheet_path` | **Code fixed** — unlink on `cast=[]`; **needs redeploy** | | |

### Beat 0 recreate (2026-08-02)

| Beat | Issue | Action taken | Tool fix idea | Status |
|------|-------|--------------|---------------|--------|
| 0 recreate | Native `keyframes/0` empty-cast: **no people** ✓ | Kept API first frame | — | **OK** (first only) |
| 0 recreate | Native `keyframes/1` and `/2` with `cast=[]` invented children on porch (not Em/Jo) | Replaced mid/last via freeform `images/generate` + `from-media` | Mid/last continuity must honor empty cast | **Code fixed** — empty-scene edit clause + scenery dual-ref; await redeploy smoke |
| 0 recreate | Stale `cast_ref_sheet_path` remains when cast cleared | Logged | Clear sheet when `cast=[]` | **Code fixed** — `_clear_cast_ref_sheet`; await redeploy |
| 0 continuity | **Barn redesigns every keyframe** | Manual barn-lock prompts; created scenery **the farm** (id=1) from kf0 | Prop/location lock via scenery ref + mid/last scenery REWRITE | **Prepared** — scenery ref set; assign on beat after MCP `update_frame` scenery ships |

### MCP prep (2026-08-02, pre-redeploy)

- Created scenery **the farm** (id=1) with ref copied from frame 9 kf0.
- MCP `update_frame` lacked `scenery` param (fixed in repo); REST already supports `scenery`.
- Did **not** wipe barn-locked mid/last until redeploy (would regress on old code).

## Beat 1 — Em+Jo porch (id=10) — still open identity issues

| Beat | Issue | Action taken | Tool fix idea | Status |
|------|-------|--------------|---------------|--------|
| 1 | **9 keyframes** for ~15.5s beat (dense / expensive) | Left as-is for now | `keyframe_plan_times` ~5s default → ~5 slots for 15.5s | **Code fixed** — default spacing 5s; `spacing_sec=2` override |
| 1 | Em **blue eyes** → brown/dark | Edit instructions | Identity bullets from appearance on refresh | **Code fixed** — appearance+outfit in wardrobe maps; await redeploy QA |
| 1 | Jo hair / shorts / sneakers drift | Same edit pass | Stronger wardrobe lock on mid/last | **Code fixed** (prompt-level); await redeploy QA |
| 1 | Bags missing / duplicate bags | Edits added bags | Prop keep-from-previous language | **Code fixed** (prompt-level) |
| 1 | `still_edit` triplicate Em | Regenerating `keyframes/first` | Exactly-N count lock language | **Code fixed** (prompt-level); no vision reject yet |
| 1 | Em appearance_prompt lacks eye color; Jo sneakers not “white” | Logged | Sync appearance/outfit text with approved refs | Open — data fix on cast rows |
