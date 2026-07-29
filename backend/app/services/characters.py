from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Character, Project, SessionLocal, StoryboardFrame
from app.services import llm
from app.services.comfyui import ComfyUIClient
from app.services.workflows import apply_params


def _normalize_outfit(
    item: dict[str, Any] | None, *, fallback_id: str | None = None
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    oid = str(item.get("id") or fallback_id or uuid.uuid4().hex[:10]).strip()
    name = str(item.get("name") or "").strip() or "Outfit"
    prompt = str(item.get("prompt") or item.get("appearance") or "").strip()
    return {
        "id": oid,
        "name": name,
        "prompt": prompt,
        "reference_image_path": (item.get("reference_image_path") or None),
        "is_default": bool(item.get("is_default")),
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

    cast_selection: [{character_id, outfit_id|null}]. Empty/None → full project cast
    with each character's default outfit (if any).
    """
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        chars = sorted(p.characters, key=lambda x: x.position)
        by_id = {c.id: c for c in chars}

        selected: list[tuple[Character, str | None]] = []
        if cast_selection:
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
        else:
            selected = [(c, None) for c in chars]

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
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        selection = list(getattr(f, "cast", None) or [])
    return cast_sheet_for_project(
        project_id, cast_selection=selection if selection else None
    )


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
    """
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        chars = sorted(p.characters, key=lambda x: x.position)
        by_id = {c.id: c for c in chars}
        selection: list[dict[str, Any]] | None = None
        if frame_id is not None:
            f = db.get(StoryboardFrame, frame_id)
            if not f or f.project_id != project_id:
                raise KeyError(f"frame {frame_id} not found")
            selection = list(getattr(f, "cast", None) or []) or None

        selected: list[tuple[Character, str | None]] = []
        if selection:
            for item in selection:
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
) -> Path:
    """Left = current scene, right = character lock (iterative Flux ReferenceLatent)."""
    from PIL import Image

    half = width // 2
    canvas = Image.new("RGB", (width, height), (20, 20, 24))
    left = _fit_cover(Image.open(scene), half, height, prefer_upper=False)
    right = _fit_cover(Image.open(character_ref), half, height, prefer_upper=True)
    canvas.paste(left, (0, 0))
    canvas.paste(right, (half, 0))
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


def pick_character_reference_path(
    project_id: int, prompt: str = ""
) -> Path | None:
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
) -> list[dict[str, Any]]:
    """Extract cast from story/premise; upsert by name; set intro_frame_id when possible."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        story = (p.story or p.premise or "").strip()
        if not story:
            raise ValueError("project has no story/premise to detect characters from")

    extracted = await llm.extract_cast(story)
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
                # Only fill empty auto fields; never overwrite user-approved look.
                if c.auto_detected or not (c.appearance_prompt or "").strip():
                    if item.get("appearance_prompt"):
                        c.appearance_prompt = str(item["appearance_prompt"]).strip()
                if c.auto_detected or not (c.description or "").strip():
                    if item.get("description"):
                        c.description = str(item["description"]).strip()
                if aliases and not c.aliases:
                    c.aliases = aliases
                if intro and not c.intro_frame_id:
                    c.intro_frame_id = intro
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
        db.commit()
        return [
            _character_dict(c) for c in sorted(p.characters, key=lambda x: x.position)
        ]


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
        parts.append(
            f"Base look (override anything the instruction changes): {look}."
        )
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
    prompt = (
        f"Photorealistic character reference portrait for film continuity. "
        f"Subject: {name}. Look: {appearance}. "
        f"Neutral three-quarter pose, clear face and wardrobe, single person, full frame."
    )
    if genre:
        prompt = f"{genre} genre. {prompt}"
    if premise:
        prompt = f"Film continuity for: {premise[:200]}. {prompt}"
    if instruction and not old_ref:
        prompt = f"{prompt} Additional direction: {instruction.strip()}"

    comfy = ComfyUIClient()
    # Fresh seed on every edit so ReferenceLatent does not stick to one sample.
    seed = secrets.randbelow(2**31 - 1) ^ (int(time.time()) & 0xFFFF)
    if old_ref and instruction:
        src = _resolve_media_file(old_ref)
        uploaded = await comfy.upload_image(src)
        edit_prompt = build_character_edit_prompt(
            instruction=instruction.strip(),
            name=name,
            appearance=appearance,
        )
        neg = (
            still_negative_prompt(edit_prompt)
            + ", ignoring the edit instruction, identical copy of the reference, "
            "unchanged face shape"
        )
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
        if instruction and (instruction or "").strip():
            ch.appearance_prompt = _merge_appearance_with_edit(
                ch.appearance_prompt or appearance or "",
                instruction.strip(),
            )
        db.commit()
        db.refresh(ch)
        return _character_dict(ch)


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
        if edit_instr and not outfit_ref:
            raise ValueError("generate an outfit look before applying an edit")

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
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": still_negative_prompt(edit_prompt)
                + ", ignoring the edit instruction, identical copy of the reference, "
                "naked, lingerie, wrong outfit",
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
            f"Dress {name} in this outfit ({outfit_name}): {wardrobe}. "
            "Full body or three-quarter view so clothing is clear; keep the same person."
        )
        if edit_instr:
            dress = f"{dress} Additional direction: {edit_instr}"
        if char_ref:
            src = _resolve_media_file(char_ref)
            uploaded = await comfy.upload_image(src)
            edit_prompt = build_character_edit_prompt(
                instruction=dress,
                name=name,
                appearance=appearance,
            )
            graph = apply_params(
                "still_edit",
                {
                    "positive_prompt": edit_prompt,
                    "negative_prompt": still_negative_prompt(edit_prompt)
                    + ", naked, lingerie, wrong outfit, ignoring wardrobe instruction",
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
                f"Photorealistic character wardrobe reference. Subject: {name}. "
                f"Look: {appearance}. Outfit ({outfit_name}): {wardrobe}. "
                "Clear full-body or three-quarter pose, plain background, single person."
            )
            if edit_instr:
                prompt = f"{prompt} Additional direction: {edit_instr}"
            graph = apply_params(
                "still_hero",
                {
                    "positive_prompt": prompt,
                    "negative_prompt": still_negative_prompt(prompt),
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
                if edit_instr:
                    o["prompt"] = _merge_appearance_with_edit(
                        o.get("prompt") or wardrobe or "",
                        edit_instr,
                    )
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
