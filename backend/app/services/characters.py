from __future__ import annotations

import re
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Character, Project, SessionLocal, StoryboardFrame
from app.services import llm
from app.services.comfyui import ComfyUIClient
from app.services.workflows import apply_params

# Outfit created automatically from the character portrait reference.
_PORTRAIT_OUTFIT_SOURCE = "character_reference"

_TITLE_TOKENS = frozenset(
    {
        "aunt",
        "uncle",
        "mr",
        "mrs",
        "ms",
        "miss",
        "dr",
        "sir",
        "lady",
        "lord",
        "the",
    }
)


def character_mentioned_in_prompt(
    name: str,
    prompt: str,
    *,
    aliases: list[str] | None = None,
) -> bool:
    """True when the character's name or alias appears in the prompt text."""
    text = prompt or ""
    if not text.strip():
        return False
    text_l = text.lower()
    candidates: list[str] = []
    raw_name = (name or "").strip()
    if raw_name:
        candidates.append(raw_name)
        for part in re.split(r"[\s/]+", raw_name):
            p = part.strip()
            if len(p) > 2 and p.lower() not in _TITLE_TOKENS:
                candidates.append(p)
    for a in aliases or []:
        a = str(a).strip()
        if a:
            candidates.append(a)
    seen: set[str] = set()
    for token in candidates:
        key = token.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if len(token) <= 2:
            if re.search(rf"(?i)\b{re.escape(token)}\b", text):
                return True
        elif key in text_l:
            return True
    return False


def filter_cast_panels_by_prompt(
    project_id: int,
    panels: list[tuple[str, Path, bool]],
    prompt: str,
    *,
    strict: bool = False,
) -> tuple[list[tuple[str, Path, bool]], list[str]]:
    """Keep panels for characters named in ``prompt``; return (kept, omitted_names).

    When ``strict`` and nothing matches, return an empty kept list (caller should not
    force the full beat cast into a spotlight keyframe). When not strict and nothing
    matches, keep all panels (hero stills with vague beats).
    """
    if not panels:
        return [], []
    aliases_by_name: dict[str, list[str]] = {}
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if p:
            for c in p.characters:
                n = (c.name or "").strip()
                if n:
                    aliases_by_name[n.lower()] = [
                        str(a).strip() for a in (c.aliases or []) if str(a).strip()
                    ]
    kept: list[tuple[str, Path, bool]] = []
    omitted: list[str] = []
    for label, path, approved in panels:
        name = (label.split("/")[0].strip() or label).strip()
        aliases = aliases_by_name.get(name.lower(), [])
        if character_mentioned_in_prompt(name, prompt, aliases=aliases):
            kept.append((label, path, approved))
        else:
            omitted.append(name)
    if kept:
        return kept, omitted
    if strict:
        all_names = [
            (label.split("/")[0].strip() or label).strip() for label, _p, _a in panels
        ]
        return [], [n for n in all_names if n]
    return panels, []


def new_cast_panels_vs_prompt(
    project_id: int,
    panels: list[tuple[str, Path, bool]],
    current_prompt: str,
    previous_prompt: str | None,
) -> list[tuple[str, Path, bool]]:
    """Panels named in ``current_prompt`` that were not named in ``previous_prompt``.

    Used when a mid/last keyframe introduces someone new (e.g. \"Jo enters\") so we
    can cast-lock that identity into the previous still instead of inventing them.
    """
    current, _omitted = filter_cast_panels_by_prompt(
        project_id, panels, current_prompt, strict=True
    )
    if not current:
        return []
    prev_text = (previous_prompt or "").strip()
    if not prev_text:
        return current
    previous, _ = filter_cast_panels_by_prompt(
        project_id, panels, prev_text, strict=True
    )
    prev_names = {
        (lab.split("/")[0].strip() or lab).strip().lower() for lab, _p, _a in previous
    }
    return [
        panel
        for panel in current
        if (panel[0].split("/")[0].strip() or panel[0]).strip().lower()
        not in prev_names
    ]


def frame_cast_selection(frame: StoryboardFrame) -> list[dict[str, Any]]:
    """Beat cast list. Empty list means no people — never treat as 'use full project'."""
    raw = getattr(frame, "cast", None)
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def cast_sheet_for_named_characters(
    project_id: int,
    frame_id: int,
    names: list[str],
) -> str:
    """Cast-sheet text limited to the given character names (case-insensitive)."""
    want = {n.strip().lower() for n in names if (n or "").strip()}
    if not want:
        return ""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        selection = frame_cast_selection(f)
    # Prefer beat cast; if beat cast is empty, resolve names from full project
    # (only when caller already decided those names are on-screen).
    entries = cast_entries_for_sheet(
        project_id, cast_selection=selection if selection else None
    )
    filtered = [e for e in entries if (e.get("name") or "").strip().lower() in want]
    return llm.format_cast_sheet(filtered)


def _normalize_outfit(
    item: dict[str, Any] | None, *, fallback_id: str | None = None
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    oid = str(item.get("id") or fallback_id or uuid.uuid4().hex[:10]).strip()
    name = str(item.get("name") or "").strip() or "Outfit"
    prompt = str(item.get("prompt") or item.get("appearance") or "").strip()
    source = str(item.get("source") or "").strip() or None
    return {
        "id": oid,
        "name": name,
        "prompt": prompt,
        "reference_image_path": (item.get("reference_image_path") or None),
        "is_default": bool(item.get("is_default")),
        "source": source,
    }


def _normalize_outfits(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        norm = _normalize_outfit(item if isinstance(item, dict) else None)
        if norm and (norm["name"] or norm["prompt"]):
            out.append(norm)
    if out:
        defaults = [o for o in out if o.get("is_default")]
        if not defaults:
            out[0]["is_default"] = True
        elif len(defaults) > 1:
            keep = defaults[0]["id"]
            for o in out:
                o["is_default"] = o["id"] == keep
    return out


def _outfit_by_id(
    outfits: list[dict[str, Any]], outfit_id: str | None
) -> dict[str, Any] | None:
    if not outfits:
        return None
    if outfit_id:
        for o in outfits:
            if o.get("id") == outfit_id:
                return o
    for o in outfits:
        if o.get("is_default"):
            return o
    return outfits[0]


def _character_dict(c: Character) -> dict[str, Any]:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "position": c.position,
        "name": c.name or "",
        "aliases": list(c.aliases or []),
        "description": c.description or "",
        "appearance_prompt": c.appearance_prompt or "",
        "outfits": _normalize_outfits(getattr(c, "outfits", None) or []),
        "reference_image_path": c.reference_image_path,
        "intro_frame_id": c.intro_frame_id,
        "auto_detected": bool(c.auto_detected),
        "approved": bool(c.approved),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def list_characters(project_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        return [
            _character_dict(c) for c in sorted(p.characters, key=lambda x: x.position)
        ]


def cast_entries_for_sheet(
    project_id: int,
    *,
    cast_selection: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve characters (+ optional outfit) for cast-sheet formatting.

    cast_selection semantics:
    - ``None`` → full project cast (project-wide / legacy callers)
    - ``[]`` → nobody (explicit empty beat cast)
    - ``[{character_id, outfit_id|null}, ...]`` → those characters only
    """
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        chars = sorted(p.characters, key=lambda x: x.position)
        by_id = {c.id: c for c in chars}

        selected: list[tuple[Character, str | None]] = []
        if cast_selection is None:
            selected = [(c, None) for c in chars]
        else:
            for item in cast_selection:
                if not isinstance(item, dict):
                    continue
                try:
                    cid = int(item.get("character_id"))
                except (TypeError, ValueError):
                    continue
                c = by_id.get(cid)
                if not c:
                    continue
                oid = item.get("outfit_id")
                selected.append((c, str(oid) if oid else None))

        entries: list[dict[str, Any]] = []
        for c, outfit_id in selected:
            outfits = _normalize_outfits(getattr(c, "outfits", None) or [])
            outfit = _outfit_by_id(outfits, outfit_id)
            entries.append(
                {
                    "name": c.name,
                    "appearance_prompt": c.appearance_prompt,
                    "description": c.description,
                    "wardrobe_prompt": (outfit or {}).get("prompt") or "",
                    "outfit_name": (outfit or {}).get("name") or "",
                    "outfit_id": (outfit or {}).get("id"),
                    "character_id": c.id,
                }
            )
        return entries


def cast_sheet_for_project(
    project_id: int,
    *,
    cast_selection: list[dict[str, Any]] | None = None,
) -> str:
    return llm.format_cast_sheet(
        cast_entries_for_sheet(project_id, cast_selection=cast_selection)
    )


def cast_sheet_for_frame(project_id: int, frame_id: int) -> str:
    """Cast lock text for a beat. Explicit ``cast=[]`` → empty (no people)."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        selection = frame_cast_selection(f)
    if not selection:
        return ""
    return cast_sheet_for_project(project_id, cast_selection=selection)


def _resolve_ref_file(stored: str | None) -> Path | None:
    stored = str(stored or "").strip()
    if not stored:
        return None
    try:
        from app.services.storyboard import _resolve_media_file

        path = _resolve_media_file(stored)
    except (FileNotFoundError, ValueError, OSError):
        return None
    return path if path.is_file() else None


def list_cast_reference_panels(
    project_id: int,
    *,
    frame_id: int | None = None,
) -> list[tuple[str, Path, bool]]:
    """Ordered (label, image_path, approved) panels for the beat cast.

    Prefers each character's selected outfit reference, else their portrait.
    When ``frame_id`` is set and beat ``cast`` is empty, returns no panels
    (do not fall back to the full project cast).
    """
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        chars = sorted(p.characters, key=lambda x: x.position)
        by_id = {c.id: c for c in chars}
        selected: list[tuple[Character, str | None]] = []
        if frame_id is not None:
            f = db.get(StoryboardFrame, frame_id)
            if not f or f.project_id != project_id:
                raise KeyError(f"frame {frame_id} not found")
            selection = frame_cast_selection(f)
            if not selection:
                return []
            for item in selection:
                try:
                    cid = int(item.get("character_id"))
                except (TypeError, ValueError):
                    continue
                c = by_id.get(cid)
                if not c:
                    continue
                oid = item.get("outfit_id")
                selected.append((c, str(oid) if oid else None))
        else:
            selected = [(c, None) for c in chars]

        panels: list[tuple[str, Path, bool]] = []
        for c, outfit_id in selected:
            outfits = _normalize_outfits(getattr(c, "outfits", None) or [])
            outfit = _outfit_by_id(outfits, outfit_id)
            stored = ""
            name = (c.name or "Character").strip() or "Character"
            label = name
            if outfit:
                stored = str(outfit.get("reference_image_path") or "").strip()
                oname = str(outfit.get("name") or "").strip()
                if oname:
                    label = f"{name} / {oname}"
            if not stored:
                stored = str(c.reference_image_path or "").strip()
            path = _resolve_ref_file(stored)
            if path is not None:
                panels.append((label, path, bool(c.approved)))
        return panels


def _fit_cover(img, tw: int, th: int, *, prefer_upper: bool = False):
    """Scale-cover crop into tw×th. prefer_upper keeps faces for full-body refs."""
    from PIL import Image

    img = img.convert("RGB")
    scale = max(tw / img.width, th / img.height)
    nw = max(tw, int(img.width * scale + 0.5))
    nh = max(th, int(img.height * scale + 0.5))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = max(0, (nw - tw) // 2)
    if prefer_upper:
        top = max(0, min(nh - th, int((nh - th) * 0.12)))
    else:
        top = max(0, (nh - th) // 2)
    return img.crop((left, top, left + tw, top + th))


def build_cast_reference_sheet(
    panels: list[tuple[str, Path]] | list[tuple[str, Path, bool]],
    dest: Path,
    *,
    cell_w: int = 512,
    cell_h: int = 576,
    label_h: int = 36,
    gap: int = 12,
    max_cols: int = 3,
    labels: bool = True,
) -> Path:
    """Composite cast/outfit refs into one contact sheet (UI / debugging)."""
    from PIL import Image, ImageDraw, ImageFont

    normalized: list[tuple[str, Path]] = []
    for item in panels:
        if len(item) >= 2:
            normalized.append((str(item[0]), item[1]))
    if not normalized:
        raise ValueError("no cast reference panels to composite")

    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(normalized) == 1:
        img = Image.open(normalized[0][1]).convert("RGB")
        img.save(dest)
        return dest

    n = len(normalized)
    cols = min(max_cols, n)
    rows = (n + cols - 1) // cols
    lh = label_h if labels else 0
    sheet_w = cols * cell_w + (cols + 1) * gap
    sheet_h = rows * (cell_h + lh) + (rows + 1) * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except OSError:
        font = None

    for i, (label, path) in enumerate(normalized):
        r, c = divmod(i, cols)
        x0 = gap + c * (cell_w + gap)
        y0 = gap + r * (cell_h + lh + gap)
        src = _fit_cover(Image.open(path), cell_w, cell_h, prefer_upper=True)
        sheet.paste(src, (x0, y0))
        if labels:
            text = (label or "")[:48]
            if text:
                ty = y0 + cell_h + 6
                if font is not None:
                    draw.text((x0 + 4, ty), text, fill=(230, 230, 235), font=font)
                else:
                    draw.text((x0 + 4, ty), text, fill=(230, 230, 235))

    sheet.save(dest)
    return dest


def build_identity_pair_sheet(
    scene: Path,
    character_ref: Path,
    dest: Path,
    *,
    width: int = 1024,
    height: int = 576,
    character_ratio: float = 0.28,
) -> Path:
    """Left = current scene, right = narrow character lock (Flux ReferenceLatent).

    Keep the right strip slim so the model weights the scene layout and is less likely
    to emit a literal split-screen copy of the guide.
    """
    from PIL import Image

    # Cap the strip well below 1/3 — wider strips were often copied as a second panel.
    right_w = max(width // 6, min(int(width * 0.30), int(width * character_ratio)))
    left_w = width - right_w
    canvas = Image.new("RGB", (width, height), (20, 20, 24))
    left = _fit_cover(Image.open(scene), left_w, height, prefer_upper=False)
    right = _fit_cover(Image.open(character_ref), right_w, height, prefer_upper=True)
    canvas.paste(left, (0, 0))
    canvas.paste(right, (left_w, 0))
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest)
    return dest


def prepare_single_ref_canvas(
    src: Path,
    dest: Path,
    *,
    width: int = 1024,
    height: int = 576,
) -> Path:
    """Face-weighted 16:9 canvas so ImageScale center-crop does not chop heads."""
    from PIL import Image

    out = _fit_cover(Image.open(src), width, height, prefer_upper=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.save(dest)
    return dest


def cast_reference_for_frame(
    project_id: int,
    frame_id: int,
    *,
    dest: Path,
) -> Path | None:
    """Build cast/outfit reference image for a beat (contact sheet when 2+).

    Returns None when no character/outfit refs exist for the selected cast.
    """
    panels = list_cast_reference_panels(project_id, frame_id=frame_id)
    if not panels:
        return None
    return build_cast_reference_sheet(panels, dest)


def pick_cast_reference_path(
    project_id: int,
    *,
    frame_id: int | None = None,
    prompt: str = "",
) -> Path | None:
    """Best single reference still when only one identity can be locked.

    Prefer ``cast_reference_for_frame`` for multi-cast beats.
    """
    panels = list_cast_reference_panels(project_id, frame_id=frame_id)
    if not panels:
        return None
    if len(panels) == 1:
        return panels[0][1]

    prompt_l = (prompt or "").lower()
    matched = [
        (label, path)
        for label, path, _approved in panels
        if any(
            part and part in prompt_l
            for part in label.lower().replace("/", " ").split()
        )
    ]
    # Prefer exact character-name hits (first token before " / ")
    name_hits = [
        (label, path)
        for label, path, _a in panels
        if (label.split("/")[0].strip().lower() in prompt_l)
    ]
    pool = name_hits or matched
    if len(pool) == 1:
        return pool[0][1]
    if len(pool) > 1:
        return pool[0][1]

    approved = [(label, path) for label, path, a in panels if a]
    if len(approved) == 1:
        return approved[0][1]
    return None


def pick_character_reference_path(project_id: int, prompt: str = "") -> Path | None:
    """Best character reference still for locking identity in a keyframe/still render.

    Prefers a name/alias mentioned in the prompt; otherwise a sole cast member with a
    reference, or a single approved reference when several exist.
    """
    return pick_cast_reference_path(project_id, prompt=prompt)


def create_character(
    project_id: int,
    *,
    name: str,
    description: str = "",
    appearance_prompt: str = "",
    aliases: list[str] | None = None,
    outfits: list[dict[str, Any]] | None = None,
    auto_detected: bool = False,
    approved: bool = False,
    intro_frame_id: int | None = None,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("character name is required")
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        pos = len(p.characters)
        c = Character(
            project_id=project_id,
            position=pos,
            name=name,
            aliases=list(aliases or []),
            description=(description or "").strip(),
            appearance_prompt=(appearance_prompt or "").strip(),
            outfits=_normalize_outfits(outfits or []),
            auto_detected=auto_detected,
            approved=approved,
            intro_frame_id=intro_frame_id,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return _character_dict(c)


def update_character(
    project_id: int, character_id: int, **fields: Any
) -> dict[str, Any]:
    allowed = {
        "name",
        "description",
        "appearance_prompt",
        "outfits",
        "aliases",
        "position",
        "intro_frame_id",
        "auto_detected",
        "approved",
        "reference_image_path",
    }
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "aliases":
                if not isinstance(v, list):
                    raise ValueError("aliases must be a list")
                c.aliases = [str(a).strip() for a in v if str(a).strip()]
            elif k == "outfits":
                if not isinstance(v, list):
                    raise ValueError("outfits must be a list")
                c.outfits = _normalize_outfits(v)
                c.auto_detected = False
            elif k == "name":
                name = str(v).strip()
                if not name:
                    raise ValueError("character name is required")
                c.name = name
                c.auto_detected = False
            else:
                setattr(c, k, v)
                if k in ("description", "appearance_prompt"):
                    c.auto_detected = False
        db.commit()
        db.refresh(c)
        return _character_dict(c)


def delete_character(project_id: int, character_id: int) -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        ref = c.reference_image_path
        db.delete(c)
        db.commit()
    if ref:
        try:
            from app.services.storyboard import _resolve_media_file

            path = _resolve_media_file(ref)
            if path.is_file() and str(path.resolve()).startswith(
                str(settings.media_dir.resolve())
            ):
                path.unlink(missing_ok=True)
        except (FileNotFoundError, ValueError, OSError):
            pass
    return {"deleted": True, "character_id": character_id}


def _norm_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


_SPACESUIT_RE = re.compile(
    r"(?i)\b(spacesuit|space\s*suit|eva\s*suit|helmet|bubble\s*helmet|"
    r"neck\s*ring|glass\s*dome|visor|oxygen)\b"
)
_CASUAL_OUTFIT_RE = re.compile(
    r"(?i)\b(summer|play|everyday|casual|school|home|civilian|beach|"
    r"garden|picnic|pajamas?|pyjamas?|t-?shirt|dress|shorts)\b"
)


def _is_spacesuit_text(*parts: str) -> bool:
    return any(_SPACESUIT_RE.search(p or "") for p in parts)


def _is_casual_outfit(name: str = "", prompt: str = "") -> bool:
    blob = f"{name} {prompt}"
    if _is_spacesuit_text(blob):
        return False
    return bool(_CASUAL_OUTFIT_RE.search(blob))


def _helmet_free_negatives(*, casual: bool) -> str:
    if not casual:
        return ""
    return (
        "space helmet, bubble helmet, glass dome helmet, astronaut helmet, "
        "neck ring, EVA suit, spacesuit, oxygen hose, visor down"
    )


def _strip_applied_edits(text: str) -> str:
    """Keep face/body identity; drop Applied-edits clutter from appearance."""
    raw = (text or "").strip()
    if not raw:
        return ""
    marker = "Applied edits:"
    idx = raw.find(marker)
    if idx >= 0:
        return raw[:idx].strip()
    return raw


def heuristic_cast_names(story: str) -> list[str]:
    """Proper-name candidates from story text (fills LLM gaps)."""
    text = story or ""
    # Capitalized tokens (including multi-word like "Aunt Jo").
    found = re.findall(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b",
        text,
    )
    skip = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "then",
        "when",
        "while",
        "after",
        "before",
        "chapter",
        "episode",
        "scene",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    }
    out: list[str] = []
    seen: set[str] = set()
    for name in found:
        parts = name.split()
        if any(p.lower() in skip or p.lower() in _TITLE_TOKENS for p in parts):
            # Allow "Aunt Jo" style — keep if last token looks like a name.
            if len(parts) < 2:
                continue
            name = parts[-1]
        key = _norm_name(name)
        if len(key) < 2 or key in seen:
            continue
        # Skip all-caps shouting and single letters.
        if name.isupper() and len(name) > 3:
            continue
        seen.add(key)
        out.append(name)
    return out


def audit_outfits(project_id: int) -> dict[str, Any]:
    """Flag helmet/spacesuit tokens on outfits that look summer/everyday."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        flags: list[dict[str, Any]] = []
        for c in sorted(p.characters, key=lambda x: x.position):
            for o in _normalize_outfits(c.outfits or []):
                oname = str(o.get("name") or "")
                prompt = str(o.get("prompt") or "")
                if _is_casual_outfit(oname, prompt) and _is_spacesuit_text(
                    oname, prompt
                ):
                    flags.append(
                        {
                            "character_id": c.id,
                            "character_name": c.name,
                            "outfit_id": o.get("id"),
                            "outfit_name": oname,
                            "issue": "casual_outfit_has_spacesuit_tokens",
                            "prompt": prompt,
                        }
                    )
                elif _is_casual_outfit(oname, prompt) is False and _CASUAL_OUTFIT_RE.search(
                    oname
                ):
                    # Name says summer but prompt is spacesuit-heavy.
                    if _is_spacesuit_text(prompt):
                        flags.append(
                            {
                                "character_id": c.id,
                                "character_name": c.name,
                                "outfit_id": o.get("id"),
                                "outfit_name": oname,
                                "issue": "named_casual_but_prompt_spacesuit",
                                "prompt": prompt,
                            }
                        )
        return {"project_id": project_id, "flags": flags, "count": len(flags)}


def _find_intro_frame_id(
    frames: list[StoryboardFrame], name: str, aliases: list[str]
) -> int | None:
    tokens = [_norm_name(name), *[_norm_name(a) for a in aliases if a]]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    for fr in sorted(frames, key=lambda x: x.position):
        blob = f"{fr.description or ''} {fr.visual_prompt or ''}".lower()
        if any(t in blob for t in tokens):
            return fr.id
    return None


async def detect_characters(
    project_id: int, *, replace_auto: bool = False
) -> dict[str, Any]:
    """Extract cast from story/premise; upsert by name; set intro_frame_id when possible."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        story = (p.story or p.premise or "").strip()
        if not story:
            raise ValueError(
                "project has no story/premise to detect characters from — "
                "save or generate a story first"
            )

    try:
        extracted = await llm.extract_cast(story)
    except Exception:
        extracted = []
    # Heuristic pass fills names the small model missed.
    known = {_norm_name(str(x.get("name") or "")) for x in extracted}
    for name in heuristic_cast_names(story):
        key = _norm_name(name)
        if key in known:
            continue
        extracted.append(
            {
                "name": name,
                "aliases": [],
                "description": "",
                "appearance_prompt": "",
            }
        )
        known.add(key)

    created = 0
    updated = 0
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        assert p is not None
        by_name = {_norm_name(c.name): c for c in p.characters}
        if replace_auto:
            for c in list(p.characters):
                if c.auto_detected and not c.approved and not c.reference_image_path:
                    db.delete(c)
            db.flush()
            by_name = {_norm_name(c.name): c for c in p.characters}

        next_pos = max((c.position for c in p.characters), default=-1) + 1
        frames = list(p.frames)
        for item in extracted:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            key = _norm_name(name)
            aliases = [
                str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()
            ]
            intro = _find_intro_frame_id(frames, name, aliases)
            if key in by_name:
                c = by_name[key]
                changed = False
                # Only fill empty auto fields; never overwrite user-approved look.
                if c.auto_detected or not (c.appearance_prompt or "").strip():
                    if item.get("appearance_prompt"):
                        c.appearance_prompt = str(item["appearance_prompt"]).strip()
                        changed = True
                if c.auto_detected or not (c.description or "").strip():
                    if item.get("description"):
                        c.description = str(item["description"]).strip()
                        changed = True
                if aliases and not c.aliases:
                    c.aliases = aliases
                    changed = True
                if intro and not c.intro_frame_id:
                    c.intro_frame_id = intro
                    changed = True
                if changed:
                    updated += 1
            else:
                c = Character(
                    project_id=project_id,
                    position=next_pos,
                    name=name,
                    aliases=aliases,
                    description=str(item.get("description") or "").strip(),
                    appearance_prompt=str(item.get("appearance_prompt") or "").strip(),
                    auto_detected=True,
                    approved=False,
                    intro_frame_id=intro,
                )
                db.add(c)
                by_name[key] = c
                next_pos += 1
                created += 1
        db.commit()
        characters = [
            _character_dict(c) for c in sorted(p.characters, key=lambda x: x.position)
        ]
    return {
        "characters": characters,
        "extracted": len(extracted),
        "created": created,
        "updated": updated,
    }


async def sync_intro_frames(project_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = list(p.frames)
        for c in p.characters:
            intro = _find_intro_frame_id(frames, c.name, list(c.aliases or []))
            if intro:
                c.intro_frame_id = intro
        db.commit()
        return [
            _character_dict(c) for c in sorted(p.characters, key=lambda x: x.position)
        ]


def _resolve_media_file(stored: str) -> Path:
    from app.services.storyboard import _resolve_media_file as resolve

    return resolve(stored)


def build_character_edit_prompt(
    *, instruction: str, name: str, appearance: str = ""
) -> str:
    """Prompt for editing a character reference still (stronger change emphasis)."""
    from app.services.storyboard import _truncate

    instr = (instruction or "").strip()
    if not instr:
        raise ValueError("edit instruction is required")
    subject = (name or "the character").strip()
    parts = [
        f"Edit this character reference portrait of {subject}.",
        f"REQUIRED CHANGE — make this clearly visible in the result: {instr}.",
        "Apply the requested change strongly. When the instruction alters face shape, "
        "body, hair, or wardrobe, do not keep the previous version of those features.",
        "Keep plain studio background, single person, clear face, full frame, "
        "no collage or text overlays.",
    ]
    look = _truncate((appearance or "").strip(), 280)
    if look:
        parts.append(f"Base look (override anything the instruction changes): {look}.")
    return " ".join(parts)


def _merge_appearance_with_edit(appearance: str, instruction: str) -> str:
    base = (appearance or "").strip()
    instr = (instruction or "").strip()
    if not instr:
        return base
    if not base:
        return instr
    marker = "Applied edits:"
    if marker in base:
        if instr.lower() in base.lower():
            return base
        return f"{base}\n- {instr}"
    return f"{base}\n\n{marker}\n- {instr}"


async def generate_reference(
    project_id: int,
    character_id: int,
    *,
    instruction: str | None = None,
) -> dict[str, Any]:
    """Generate or edit the character reference still used as look ground truth."""
    settings = get_settings()
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        name = c.name
        appearance = c.appearance_prompt or c.description or name
        old_ref = c.reference_image_path
        premise = p.premise or ""
        genre = p.genre or ""

    from app.services.storyboard import still_negative_prompt

    media = (
        settings.media_dir
        / "projects"
        / str(project_id)
        / "characters"
        / str(character_id)
    )
    media.mkdir(parents=True, exist_ok=True)

    # Prefer default outfit wardrobe for fresh portraits (avoid EVA helmet bleed).
    default_outfit = None
    with SessionLocal() as db:
        ch0 = db.get(Character, character_id)
        if ch0:
            outfits0 = _normalize_outfits(ch0.outfits or [])
            default_outfit = next(
                (o for o in outfits0 if o.get("is_default")),
                outfits0[0] if outfits0 else None,
            )
    wardrobe = ""
    outfit_name = ""
    if default_outfit:
        wardrobe = str(default_outfit.get("prompt") or "").strip()
        outfit_name = str(default_outfit.get("name") or "").strip()
    appearance_clean = _strip_applied_edits(appearance)
    casual = _is_casual_outfit(outfit_name, wardrobe) or not _is_spacesuit_text(
        wardrobe, appearance_clean, premise
    )
    # If default outfit is casual/summer, force helmet-free even when premise is space.
    if default_outfit and _is_casual_outfit(outfit_name, wardrobe):
        casual = True
    elif default_outfit and _is_spacesuit_text(outfit_name, wardrobe):
        casual = False

    prompt = (
        f"Character reference portrait for film continuity. "
        f"Subject: {name}. Face/body: {appearance_clean}. "
    )
    if wardrobe:
        prompt += f"Wearing ({outfit_name or 'default'}): {wardrobe}. "
        if casual:
            prompt += (
                "No helmet, no neck ring, no glass dome, no spacesuit — "
                "everyday clothes only. "
            )
    prompt += "Neutral three-quarter pose, clear face, single person, full frame."
    if genre:
        prompt = f"{genre} genre. {prompt}"
    if instruction and not old_ref:
        prompt = f"{prompt} Additional direction: {instruction.strip()}"

    comfy = ComfyUIClient()
    # Fresh seed on every edit so ReferenceLatent does not stick to one sample.
    seed = secrets.randbelow(2**31 - 1) ^ (int(time.time()) & 0xFFFF)
    helmet_neg = _helmet_free_negatives(casual=casual)
    if old_ref and instruction:
        src = _resolve_media_file(old_ref)
        uploaded = await comfy.upload_image(src)
        edit_prompt = build_character_edit_prompt(
            instruction=instruction.strip(),
            name=name,
            appearance=appearance_clean,
        )
        neg = (
            still_negative_prompt(edit_prompt)
            + ", ignoring the edit instruction, identical copy of the reference, "
            "unchanged face shape"
        )
        if helmet_neg:
            neg = f"{neg}, {helmet_neg}"
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_char{character_id}_ref",
                "width": 1024,
                "height": 576,
                "steps": 28,
                "cfg": 6.5,
            },
            uploaded_image_name=uploaded,
        )
    else:
        neg = still_negative_prompt(prompt)
        if helmet_neg:
            neg = f"{neg}, {helmet_neg}"
        graph = apply_params(
            "still_hero",
            {
                "positive_prompt": prompt,
                "negative_prompt": neg,
                "seed": seed if instruction else (character_id * 41),
                "filename_prefix": f"local_video/p{project_id}_char{character_id}_ref",
            },
        )

    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no character reference output")
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    # Remove previous reference file when replaced by an edit.
    if old_ref and instruction:
        try:
            old = _resolve_media_file(old_ref)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    with SessionLocal() as db:
        ch = db.get(Character, character_id)
        assert ch
        ch.reference_image_path = str(dest)
        ch.auto_detected = False
        # Keep appearance_prompt as face/body only — do not append Applied edits.
        if ch.appearance_prompt:
            ch.appearance_prompt = _strip_applied_edits(ch.appearance_prompt)
        appearance_now = ch.appearance_prompt or appearance_clean or appearance or ""
        name_now = ch.name or name
        db.commit()

    # Portrait → outfit: same picture + wardrobe description for cast locking.
    try:
        await _sync_outfit_from_character_reference(
            project_id,
            character_id,
            dest,
            name=name_now,
            appearance=appearance_now,
            instruction=(instruction or "").strip() or None,
        )
    except Exception:
        # Reference still succeeded; outfit sync is best-effort.
        pass

    with SessionLocal() as db:
        ch = db.get(Character, character_id)
        assert ch
        db.refresh(ch)
        return _character_dict(ch)


async def _sync_outfit_from_character_reference(
    project_id: int,
    character_id: int,
    ref_path: Path,
    *,
    name: str,
    appearance: str,
    instruction: str | None = None,
) -> None:
    """Copy the portrait into an outfit card and fill wardrobe name/prompt via LLM."""
    described = await llm.describe_outfit_from_character_look(
        name=name,
        appearance=appearance,
        extra_instruction=instruction,
    )
    outfit_name = (described.get("name") or "From portrait").strip() or "From portrait"
    wardrobe = (described.get("prompt") or "").strip()
    if not wardrobe:
        wardrobe = f"Wardrobe as shown in {name}'s character portrait"

    outfits_dir = (
        get_settings().media_dir
        / "projects"
        / str(project_id)
        / "characters"
        / str(character_id)
        / "outfits"
    )
    outfits_dir.mkdir(parents=True, exist_ok=True)
    dest = outfits_dir / f"from_portrait{ref_path.suffix or '.png'}"
    shutil.copy2(ref_path, dest)

    with SessionLocal() as db:
        ch = db.get(Character, character_id)
        if not ch or ch.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        outfits = _normalize_outfits(ch.outfits or [])
        portrait = next(
            (o for o in outfits if (o.get("source") or "") == _PORTRAIT_OUTFIT_SOURCE),
            None,
        )
        if (
            portrait is None
            and len(outfits) == 1
            and not (outfits[0].get("prompt") or "").strip()
        ):
            # Empty single placeholder outfit → claim it as the portrait outfit.
            portrait = outfits[0]
            portrait["source"] = _PORTRAIT_OUTFIT_SOURCE

        old_img = None
        if portrait is not None:
            old_img = portrait.get("reference_image_path")
            portrait["name"] = outfit_name
            portrait["prompt"] = wardrobe
            portrait["reference_image_path"] = str(dest)
            portrait["source"] = _PORTRAIT_OUTFIT_SOURCE
            if not any(o.get("is_default") for o in outfits):
                portrait["is_default"] = True
        else:
            make_default = not any(o.get("is_default") for o in outfits)
            if make_default:
                for o in outfits:
                    o["is_default"] = False
            outfits.append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "name": outfit_name,
                    "prompt": wardrobe,
                    "reference_image_path": str(dest),
                    "is_default": make_default or not outfits,
                    "source": _PORTRAIT_OUTFIT_SOURCE,
                }
            )
        ch.outfits = list(outfits)
        db.commit()

    if old_img:
        try:
            prev = _resolve_media_file(str(old_img))
            if prev.resolve() != dest.resolve() and prev.is_file():
                # Don't delete if it is the live character portrait path.
                if prev.resolve() != ref_path.resolve():
                    prev.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass


def delete_reference(project_id: int, character_id: int) -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        old = c.reference_image_path
        c.reference_image_path = None
        db.commit()
        db.refresh(c)
        payload = _character_dict(c)
    if old:
        try:
            path = _resolve_media_file(old)
            if path.is_file() and str(path.resolve()).startswith(
                str(settings.media_dir.resolve())
            ):
                path.unlink(missing_ok=True)
        except (FileNotFoundError, ValueError, OSError):
            pass
    return payload


async def generate_outfit_reference(
    project_id: int,
    character_id: int,
    outfit_id: str,
    *,
    instruction: str | None = None,
) -> dict[str, Any]:
    """Generate or prompt-edit a wardrobe reference for one outfit.

    - With instruction + existing outfit still → edit that still (like character refs).
    - Otherwise dress from the character reference when available, else still_hero.
    """
    settings = get_settings()
    edit_instr = (instruction or "").strip() or None
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        outfits = _normalize_outfits(c.outfits or [])
        outfit = next((o for o in outfits if o.get("id") == outfit_id), None)
        if not outfit:
            raise KeyError(f"outfit {outfit_id} not found")
        name = c.name
        appearance = c.appearance_prompt or c.description or name
        char_ref = c.reference_image_path
        outfit_ref = outfit.get("reference_image_path")
        wardrobe = (outfit.get("prompt") or "").strip()
        outfit_name = outfit.get("name") or "Outfit"
        if not wardrobe and not edit_instr:
            raise ValueError("outfit prompt is empty — describe the clothing first")
        # First generate: treat instruction as part of the create prompt (not an edit).

    from app.services.storyboard import still_negative_prompt

    media = (
        settings.media_dir
        / "projects"
        / str(project_id)
        / "characters"
        / str(character_id)
        / "outfits"
    )
    media.mkdir(parents=True, exist_ok=True)
    comfy = ComfyUIClient()
    seed = secrets.randbelow(2**31 - 1) ^ (int(time.time()) & 0xFFFF)
    prefix = f"local_video/p{project_id}_char{character_id}_outfit_{outfit_id}"
    casual = _is_casual_outfit(outfit_name, wardrobe) or (
        bool(edit_instr) and _is_casual_outfit(outfit_name, edit_instr)
    )
    helmet_neg = _helmet_free_negatives(casual=casual)
    casual_instr = (
        " No helmet, no neck ring, no glass dome, no spacesuit."
        if casual
        else ""
    )

    if edit_instr and outfit_ref:
        src = _resolve_media_file(str(outfit_ref))
        uploaded = await comfy.upload_image(src)
        edit_prompt = build_character_edit_prompt(
            instruction=edit_instr,
            name=name,
            appearance=f"{appearance}. Current outfit ({outfit_name}): {wardrobe}"
            if wardrobe
            else appearance,
        )
        neg = (
            still_negative_prompt(edit_prompt)
            + ", ignoring the edit instruction, identical copy of the reference, "
            "naked, lingerie, wrong outfit"
        )
        if helmet_neg:
            neg = f"{neg}, {helmet_neg}"
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt + casual_instr,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": prefix,
                "width": 1024,
                "height": 576,
                "steps": 28,
                "cfg": 6.5,
            },
            uploaded_image_name=uploaded,
        )
    else:
        dress = (
            f"Dress {name} in this outfit ({outfit_name}): {wardrobe or edit_instr}. "
            "Full body or three-quarter view so clothing is clear; keep the same person."
            f"{casual_instr}"
        )
        if edit_instr and wardrobe:
            dress = f"{dress} Additional direction: {edit_instr}"
        if char_ref:
            src = _resolve_media_file(char_ref)
            uploaded = await comfy.upload_image(src)
            edit_prompt = build_character_edit_prompt(
                instruction=dress,
                name=name,
                appearance=_strip_applied_edits(appearance),
            )
            neg = (
                still_negative_prompt(edit_prompt)
                + ", naked, lingerie, wrong outfit, ignoring wardrobe instruction"
            )
            if helmet_neg:
                neg = f"{neg}, {helmet_neg}"
            graph = apply_params(
                "still_edit",
                {
                    "positive_prompt": edit_prompt,
                    "negative_prompt": neg,
                    "seed": seed,
                    "filename_prefix": prefix,
                    "width": 1024,
                    "height": 576,
                    "steps": 28,
                    "cfg": 6.5,
                },
                uploaded_image_name=uploaded,
            )
        else:
            prompt = (
                f"Character wardrobe reference. Subject: {name}. "
                f"Look: {_strip_applied_edits(appearance)}. "
                f"Outfit ({outfit_name}): {wardrobe or edit_instr}. "
                f"Clear full-body or three-quarter pose, plain background, single person."
                f"{casual_instr}"
            )
            if edit_instr and wardrobe:
                prompt = f"{prompt} Additional direction: {edit_instr}"
            neg = still_negative_prompt(prompt)
            if helmet_neg:
                neg = f"{neg}, {helmet_neg}"
            graph = apply_params(
                "still_hero",
                {
                    "positive_prompt": prompt,
                    "negative_prompt": neg,
                    "seed": seed,
                    "filename_prefix": prefix,
                },
            )

    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outfit reference output")
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    with SessionLocal() as db:
        ch = db.get(Character, character_id)
        assert ch
        outfits = _normalize_outfits(ch.outfits or [])
        updated = False
        for o in outfits:
            if o.get("id") == outfit_id:
                old = o.get("reference_image_path")
                o["reference_image_path"] = str(dest)
                # Clothing-only: merge instruction into outfit prompt, never appearance.
                if edit_instr and not (o.get("prompt") or "").strip():
                    o["prompt"] = edit_instr.strip()
                elif edit_instr and outfit_ref:
                    # Edits of existing looks can refine wardrobe text lightly.
                    base_w = (o.get("prompt") or wardrobe or "").strip()
                    if edit_instr.strip().lower() not in base_w.lower():
                        o["prompt"] = f"{base_w}. {edit_instr.strip()}".strip(". ")
                updated = True
                if old:
                    try:
                        prev = _resolve_media_file(str(old))
                        if prev.resolve() != dest.resolve() and prev.is_file():
                            prev.unlink()
                    except (FileNotFoundError, ValueError, OSError):
                        pass
                break
        if not updated:
            raise KeyError(f"outfit {outfit_id} not found")
        ch.outfits = list(outfits)
        ch.auto_detected = False
        db.commit()
        db.refresh(ch)
        return _character_dict(ch)


def set_character_reference_from_media(
    project_id: int, character_id: int, media_path: str
) -> dict[str, Any]:
    """Copy a library/media image into the character folder and set as portrait."""
    from app.services.library import copy_media_into

    path = (media_path or "").strip()
    if not path:
        raise ValueError("media_path is required")
    dest_dir = (
        get_settings().media_dir
        / "projects"
        / str(project_id)
        / "characters"
        / str(character_id)
    )
    dest = copy_media_into(path, dest_dir, filename=f"ref_{Path(path).name}")
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        old = c.reference_image_path
        c.reference_image_path = str(dest)
        c.auto_detected = False
        db.commit()
        db.refresh(c)
        payload = _character_dict(c)
    if old:
        try:
            prev = _resolve_media_file(str(old))
            if prev.resolve() != dest.resolve() and prev.is_file():
                prev.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass
    return payload


def set_outfit_reference_from_media(
    project_id: int,
    character_id: int,
    outfit_id: str,
    media_path: str,
) -> dict[str, Any]:
    """Copy a library/media image into the outfit folder and set as wardrobe still."""
    from app.services.library import copy_media_into

    path = (media_path or "").strip()
    if not path:
        raise ValueError("media_path is required")
    dest_dir = (
        get_settings().media_dir
        / "projects"
        / str(project_id)
        / "characters"
        / str(character_id)
        / "outfits"
    )
    dest = copy_media_into(
        path, dest_dir, filename=f"outfit_{outfit_id}_{Path(path).name}"
    )
    with SessionLocal() as db:
        c = db.get(Character, character_id)
        if not c or c.project_id != project_id:
            raise KeyError(f"character {character_id} not found")
        outfits = _normalize_outfits(c.outfits or [])
        old = None
        found = False
        for o in outfits:
            if o.get("id") == outfit_id:
                old = o.get("reference_image_path")
                o["reference_image_path"] = str(dest)
                found = True
                break
        if not found:
            raise KeyError(f"outfit {outfit_id} not found")
        c.outfits = list(outfits)
        c.auto_detected = False
        db.commit()
        db.refresh(c)
        payload = _character_dict(c)
    if old:
        try:
            prev = _resolve_media_file(str(old))
            if prev.resolve() != dest.resolve() and prev.is_file():
                prev.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass
    return payload
