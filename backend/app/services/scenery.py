"""Project scenery / locations — visual ground truth for sets and environments."""

from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Project, Scenery, SessionLocal, StoryboardFrame
from app.services.comfyui import ComfyUIClient
from app.services.workflows import apply_params


def _normalize_variant(
    item: dict[str, Any] | None, *, fallback_id: str | None = None
) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    oid = str(item.get("id") or fallback_id or uuid.uuid4().hex[:10]).strip()
    name = str(item.get("name") or "").strip() or "Variant"
    prompt = str(item.get("prompt") or item.get("appearance") or "").strip()
    return {
        "id": oid,
        "name": name,
        "prompt": prompt,
        "reference_image_path": (item.get("reference_image_path") or None),
        "is_default": bool(item.get("is_default")),
    }


def _normalize_variants(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        norm = _normalize_variant(item if isinstance(item, dict) else None)
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


def _variant_by_id(
    variants: list[dict[str, Any]], variant_id: str | None
) -> dict[str, Any] | None:
    if not variants:
        return None
    if variant_id:
        for o in variants:
            if o.get("id") == variant_id:
                return o
    for o in variants:
        if o.get("is_default"):
            return o
    return variants[0]


def _scenery_dict(s: Scenery) -> dict[str, Any]:
    return {
        "id": s.id,
        "project_id": s.project_id,
        "position": s.position,
        "name": s.name or "",
        "aliases": list(s.aliases or []),
        "description": s.description or "",
        "appearance_prompt": s.appearance_prompt or "",
        "variants": _normalize_variants(getattr(s, "variants", None) or []),
        "reference_image_path": s.reference_image_path,
        "approved": bool(s.approved),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def list_scenery(project_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        return [
            _scenery_dict(s) for s in sorted(p.scenery, key=lambda x: x.position)
        ]


def create_scenery(
    project_id: int,
    *,
    name: str,
    description: str = "",
    appearance_prompt: str = "",
    aliases: list[str] | None = None,
    variants: list[dict[str, Any]] | None = None,
    approved: bool = False,
) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("scenery name is required")
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        pos = len(p.scenery)
        s = Scenery(
            project_id=project_id,
            position=pos,
            name=name,
            aliases=list(aliases or []),
            description=(description or "").strip(),
            appearance_prompt=(appearance_prompt or "").strip(),
            variants=_normalize_variants(variants or []),
            approved=approved,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return _scenery_dict(s)


def update_scenery(project_id: int, scenery_id: int, **fields: Any) -> dict[str, Any]:
    allowed = {
        "name",
        "description",
        "appearance_prompt",
        "variants",
        "aliases",
        "position",
        "approved",
        "reference_image_path",
    }
    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "aliases":
                if not isinstance(v, list):
                    raise ValueError("aliases must be a list")
                s.aliases = [str(a).strip() for a in v if str(a).strip()]
            elif k == "variants":
                if not isinstance(v, list):
                    raise ValueError("variants must be a list")
                s.variants = _normalize_variants(v)
            elif k == "name":
                name = str(v).strip()
                if not name:
                    raise ValueError("scenery name is required")
                s.name = name
            else:
                setattr(s, k, v)
        db.commit()
        db.refresh(s)
        return _scenery_dict(s)


def delete_scenery(project_id: int, scenery_id: int) -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
        ref = s.reference_image_path
        variants = list(s.variants or [])
        db.delete(s)
        db.commit()
    for stored in [ref, *[v.get("reference_image_path") for v in variants if isinstance(v, dict)]]:
        if not stored:
            continue
        try:
            from app.services.storyboard import _resolve_media_file

            path = _resolve_media_file(str(stored))
            if path.is_file() and str(path.resolve()).startswith(
                str(settings.media_dir.resolve())
            ):
                path.unlink(missing_ok=True)
        except (FileNotFoundError, ValueError, OSError):
            pass
    return {"deleted": True, "scenery_id": scenery_id}


def frame_scenery_selection(frame: StoryboardFrame) -> list[dict[str, Any]]:
    raw = getattr(frame, "scenery", None)
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def format_scenery_sheet(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for e in entries:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        look = (e.get("appearance_prompt") or e.get("description") or "").strip()
        variant = (e.get("variant_prompt") or "").strip()
        vname = (e.get("variant_name") or "").strip()
        bits: list[str] = []
        if look:
            bits.append(f"Look: {look}")
        if variant:
            label = f"Variant ({vname})" if vname else "Variant"
            bits.append(f"{label}: {variant}")
        if bits:
            lines.append(f"- {name}: " + ". ".join(bits))
        else:
            lines.append(f"- {name}")
    if not lines:
        return ""
    return "Scenery lock (match these exact locations):\n" + "\n".join(lines)


def scenery_entries_for_sheet(
    project_id: int,
    *,
    scenery_selection: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve scenery (+ optional variant) for prompt lock text."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        items = sorted(p.scenery, key=lambda x: x.position)
        by_id = {s.id: s for s in items}
        selected: list[tuple[Scenery, str | None]] = []
        if scenery_selection is None:
            selected = [(s, None) for s in items]
        else:
            for item in scenery_selection:
                if not isinstance(item, dict):
                    continue
                try:
                    sid = int(item.get("scenery_id"))
                except (TypeError, ValueError):
                    continue
                s = by_id.get(sid)
                if not s:
                    continue
                vid = item.get("variant_id")
                selected.append((s, str(vid) if vid else None))
        entries: list[dict[str, Any]] = []
        for s, variant_id in selected:
            variants = _normalize_variants(getattr(s, "variants", None) or [])
            variant = _variant_by_id(variants, variant_id)
            entries.append(
                {
                    "name": s.name,
                    "appearance_prompt": s.appearance_prompt,
                    "description": s.description,
                    "variant_prompt": (variant or {}).get("prompt") or "",
                    "variant_name": (variant or {}).get("name") or "",
                    "variant_id": (variant or {}).get("id"),
                    "scenery_id": s.id,
                }
            )
        return entries


def scenery_sheet_for_frame(project_id: int, frame_id: int) -> str:
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        selection = frame_scenery_selection(f)
    if not selection:
        return ""
    return format_scenery_sheet(
        scenery_entries_for_sheet(project_id, scenery_selection=selection)
    )


def scenery_sheet_for_project(project_id: int) -> str:
    return format_scenery_sheet(scenery_entries_for_sheet(project_id))


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


def resolve_scenery_reference_for_frame(
    project_id: int, frame_id: int
) -> Path | None:
    """Best location still for this beat (variant ref, else scenery portrait)."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        selection = frame_scenery_selection(f)
        if not selection:
            return None
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        by_id = {s.id: s for s in p.scenery}
        for item in selection:
            try:
                sid = int(item.get("scenery_id"))
            except (TypeError, ValueError):
                continue
            s = by_id.get(sid)
            if not s:
                continue
            variants = _normalize_variants(getattr(s, "variants", None) or [])
            vid = item.get("variant_id")
            variant = _variant_by_id(variants, str(vid) if vid else None)
            stored = ""
            if variant:
                stored = str(variant.get("reference_image_path") or "").strip()
            if not stored:
                stored = str(s.reference_image_path or "").strip()
            path = _resolve_ref_file(stored)
            if path is not None:
                return path
    return None


async def generate_reference(
    project_id: int,
    scenery_id: int,
    *,
    instruction: str | None = None,
) -> dict[str, Any]:
    """Generate or edit the scenery establishing reference still."""
    settings = get_settings()
    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        name = s.name
        appearance = s.appearance_prompt or s.description or name
        old_ref = s.reference_image_path
        genre = p.genre or ""

    from app.services.storyboard import (
        _EDIT_CLOSE,
        empty_scene_negatives,
        still_negative_prompt,
    )

    media = (
        settings.media_dir / "projects" / str(project_id) / "scenery" / str(scenery_id)
    )
    media.mkdir(parents=True, exist_ok=True)

    prompt = (
        f"Location / set reference plate for film continuity. "
        f"Place: {name}. {appearance}. "
        "Empty establishing shot — no people, no characters, no faces; "
        "architecture, props, and landscape only. One continuous camera frame."
    )
    if genre:
        prompt = f"{genre} genre. {prompt}"
    if instruction and not old_ref:
        prompt = f"{prompt} Additional direction: {instruction.strip()}"

    comfy = ComfyUIClient()
    seed = secrets.randbelow(2**31 - 1) ^ (int(time.time()) & 0xFFFF)
    neg = f"{still_negative_prompt(prompt)}, {empty_scene_negatives()}"

    if old_ref and instruction:
        src = _resolve_ref_file(old_ref)
        if src is None:
            raise FileNotFoundError(f"scenery reference missing: {old_ref}")
        uploaded = await comfy.upload_image(src)
        edit_prompt = (
            f"Edit this location reference of {name}. "
            f"REQUIRED CHANGE: {instruction.strip()}. "
            "Keep it an empty establishing shot with no people. "
            f"{_EDIT_CLOSE}"
        )
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_scenery{scenery_id}_ref",
                "width": 1024,
                "height": 576,
                "steps": 20,
                "cfg": 5.0,
            },
            uploaded_image_name=uploaded,
        )
    else:
        graph = apply_params(
            "still_hero",
            {
                "positive_prompt": prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_scenery{scenery_id}_ref",
                "width": 1024,
                "height": 576,
            },
        )
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs for scenery reference")
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    stored = str(dest)
    with SessionLocal() as db:
        row = db.get(Scenery, scenery_id)
        assert row is not None
        row.reference_image_path = stored
        if instruction:
            base = (row.appearance_prompt or "").strip()
            instr = instruction.strip()
            marker = "Applied edits:"
            if marker in base:
                if instr.lower() not in base.lower():
                    row.appearance_prompt = f"{base}\n- {instr}"
            elif base:
                row.appearance_prompt = f"{base}\n\n{marker}\n- {instr}"
            else:
                row.appearance_prompt = instr
        db.commit()
        db.refresh(row)
        return _scenery_dict(row)


def delete_reference(project_id: int, scenery_id: int) -> dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
        ref = s.reference_image_path
        s.reference_image_path = None
        db.commit()
        payload = _scenery_dict(s)
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
    return payload


def set_scenery_reference_from_media(
    project_id: int, scenery_id: int, media_path: str
) -> dict[str, Any]:
    from app.services import library as lib_svc

    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
    dest_dir = (
        get_settings().media_dir
        / "projects"
        / str(project_id)
        / "scenery"
        / str(scenery_id)
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = lib_svc.copy_media_into(
        media_path, dest_dir, filename=f"ref_{scenery_id}_{Path(media_path).name}"
    )
    return update_scenery(
        project_id, scenery_id, reference_image_path=str(copied)
    )


async def generate_variant_reference(
    project_id: int,
    scenery_id: int,
    variant_id: str,
    *,
    instruction: str | None = None,
) -> dict[str, Any]:
    """Generate a variant still (e.g. night / interior) from scenery look + variant prompt."""
    settings = get_settings()
    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        variants = _normalize_variants(s.variants or [])
        variant = _variant_by_id(variants, variant_id)
        if not variant or variant.get("id") != variant_id:
            # exact id required
            variant = next((v for v in variants if v.get("id") == variant_id), None)
        if not variant:
            raise KeyError(f"variant {variant_id} not found")
        name = s.name
        base_look = s.appearance_prompt or s.description or name
        vprompt = variant.get("prompt") or ""
        vname = variant.get("name") or "variant"
        old_ref = variant.get("reference_image_path") or s.reference_image_path
        genre = p.genre or ""

    from app.services.storyboard import (
        _EDIT_CLOSE,
        empty_scene_negatives,
        still_negative_prompt,
    )

    media = (
        settings.media_dir
        / "projects"
        / str(project_id)
        / "scenery"
        / str(scenery_id)
        / "variants"
    )
    media.mkdir(parents=True, exist_ok=True)

    prompt = (
        f"Location variant reference for {name} / {vname}. "
        f"Base place: {base_look}. Variant: {vprompt}. "
        "Empty establishing shot — no people. One continuous camera frame."
    )
    if genre:
        prompt = f"{genre} genre. {prompt}"
    if instruction and not old_ref:
        prompt = f"{prompt} Additional direction: {instruction.strip()}"

    comfy = ComfyUIClient()
    seed = secrets.randbelow(2**31 - 1) ^ (int(time.time()) & 0xFFFF)
    neg = f"{still_negative_prompt(prompt)}, {empty_scene_negatives()}"

    if old_ref and instruction:
        src = _resolve_ref_file(str(old_ref))
        if src is None:
            raise FileNotFoundError(f"variant reference missing: {old_ref}")
        uploaded = await comfy.upload_image(src)
        edit_prompt = (
            f"Edit this location variant ({vname}) of {name}. "
            f"REQUIRED CHANGE: {instruction.strip()}. "
            "Keep it empty of people. "
            f"{_EDIT_CLOSE}"
        )
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": (
                    f"local_video/p{project_id}_scenery{scenery_id}_v{variant_id}"
                ),
                "width": 1024,
                "height": 576,
                "steps": 20,
                "cfg": 5.0,
            },
            uploaded_image_name=uploaded,
        )
    elif old_ref and not instruction:
        # Restage variant from base scenery ref via dual-ref
        src = _resolve_ref_file(str(old_ref))
        if src is None:
            raise FileNotFoundError(f"scenery reference missing: {old_ref}")
        scene_up = await comfy.upload_image(src)
        # Use still_edit on base with variant instruction
        uploaded = scene_up
        edit_prompt = (
            f"Using this location reference of {name}: restage as {vname}. "
            f"{vprompt}. Empty establishing shot, no people. "
            f"{_EDIT_CLOSE}"
        )
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": (
                    f"local_video/p{project_id}_scenery{scenery_id}_v{variant_id}"
                ),
                "width": 1024,
                "height": 576,
                "steps": 20,
                "cfg": 5.0,
            },
            uploaded_image_name=uploaded,
        )
    else:
        graph = apply_params(
            "still_hero",
            {
                "positive_prompt": prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": (
                    f"local_video/p{project_id}_scenery{scenery_id}_v{variant_id}"
                ),
                "width": 1024,
                "height": 576,
            },
        )
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs for scenery variant")
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    with SessionLocal() as db:
        row = db.get(Scenery, scenery_id)
        assert row is not None
        variants = _normalize_variants(row.variants or [])
        for v in variants:
            if v.get("id") == variant_id:
                v["reference_image_path"] = str(dest)
                if instruction:
                    vp = (v.get("prompt") or "").strip()
                    v["prompt"] = (
                        f"{vp}\nApplied: {instruction.strip()}" if vp else instruction.strip()
                    )
                break
        row.variants = variants
        db.commit()
        db.refresh(row)
        return _scenery_dict(row)


def set_variant_reference_from_media(
    project_id: int, scenery_id: int, variant_id: str, media_path: str
) -> dict[str, Any]:
    from app.services import library as lib_svc

    with SessionLocal() as db:
        s = db.get(Scenery, scenery_id)
        if not s or s.project_id != project_id:
            raise KeyError(f"scenery {scenery_id} not found")
        variants = _normalize_variants(s.variants or [])
        if not any(v.get("id") == variant_id for v in variants):
            raise KeyError(f"variant {variant_id} not found")
    dest_dir = (
        get_settings().media_dir
        / "projects"
        / str(project_id)
        / "scenery"
        / str(scenery_id)
        / "variants"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = lib_svc.copy_media_into(
        media_path,
        dest_dir,
        filename=f"variant_{variant_id}_{Path(media_path).name}",
    )
    with SessionLocal() as db:
        row = db.get(Scenery, scenery_id)
        assert row is not None
        variants = _normalize_variants(row.variants or [])
        for v in variants:
            if v.get("id") == variant_id:
                v["reference_image_path"] = str(copied)
                break
        row.variants = variants
        db.commit()
        db.refresh(row)
        return _scenery_dict(row)
