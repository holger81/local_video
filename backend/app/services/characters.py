from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Character, Project, SessionLocal, StoryboardFrame
from app.services import llm
from app.services.comfyui import ComfyUIClient
from app.services.workflows import apply_params


def _character_dict(c: Character) -> dict[str, Any]:
    return {
        "id": c.id,
        "project_id": c.project_id,
        "position": c.position,
        "name": c.name or "",
        "aliases": list(c.aliases or []),
        "description": c.description or "",
        "appearance_prompt": c.appearance_prompt or "",
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


def cast_sheet_for_project(project_id: int) -> str:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        chars = sorted(p.characters, key=lambda x: x.position)
        return llm.format_cast_sheet(
            [
                {
                    "name": c.name,
                    "appearance_prompt": c.appearance_prompt,
                    "description": c.description,
                }
                for c in chars
            ]
        )


def pick_character_reference_path(
    project_id: int, prompt: str = ""
) -> Path | None:
    """Best character reference still for locking identity in a keyframe/still render.

    Prefers a name/alias mentioned in the prompt; otherwise a sole cast member with a
    reference, or a single approved reference when several exist.
    """
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        chars = [
            c
            for c in sorted(p.characters, key=lambda x: x.position)
            if (c.reference_image_path or "").strip()
        ]
    if not chars:
        return None

    prompt_l = (prompt or "").lower()
    matched: list[Character] = []
    for c in chars:
        names = [c.name or "", *(c.aliases or [])]
        if any(n.strip() and n.strip().lower() in prompt_l for n in names):
            matched.append(c)

    chosen: Character | None = None
    if len(matched) == 1:
        chosen = matched[0]
    elif len(matched) > 1:
        approved = [c for c in matched if c.approved]
        chosen = approved[0] if approved else matched[0]
    elif len(chars) == 1:
        chosen = chars[0]
    else:
        approved = [c for c in chars if c.approved]
        if len(approved) == 1:
            chosen = approved[0]
        else:
            return None

    stored = (chosen.reference_image_path or "").strip()
    if not stored:
        return None
    try:
        from app.services.storyboard import _resolve_media_file

        path = _resolve_media_file(stored)
    except (FileNotFoundError, ValueError, OSError):
        return None
    return path if path.is_file() else None


def create_character(
    project_id: int,
    *,
    name: str,
    description: str = "",
    appearance_prompt: str = "",
    aliases: list[str] | None = None,
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
    import secrets
    import time

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
