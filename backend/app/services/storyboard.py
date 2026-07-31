from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db.models import Project, SessionLocal, StoryboardFrame
from app.services import llm
from app.services.comfyui import ComfyUIClient
from app.services.ffmpeg import (
    concat_frame_dirs,
    concat_videos,
    encode_frames_to_mp4,
    extract_frames_from_video,
)
from app.services.workflows import apply_params, validate_frame_count


def _empty_keyframe(
    index: int, t_sec: float, role: str, prompt: str = "", path: str | None = None
) -> dict[str, Any]:
    return {
        "index": index,
        "t_sec": float(t_sec),
        "role": role,
        "image_prompt": prompt or "",
        "path": path,
    }


def _legacy_keyframes_from_columns(f: StoryboardFrame) -> list[dict[str, Any]]:
    """Build a series from old first/mid/last columns when keyframes JSON is empty."""
    out: list[dict[str, Any]] = []
    if f.keyframe_first_path or (f.keyframe_first_prompt or "").strip():
        out.append(
            _empty_keyframe(
                0, 0.0, "first", f.keyframe_first_prompt or "", f.keyframe_first_path
            )
        )
    if f.keyframe_mid_path or (f.keyframe_mid_prompt or "").strip():
        out.append(
            _empty_keyframe(
                len(out),
                2.0,
                "middle",
                f.keyframe_mid_prompt or "",
                f.keyframe_mid_path,
            )
        )
    if f.keyframe_last_path or (f.keyframe_last_prompt or "").strip():
        dur = float(f.duration_hint_sec or 4.0)
        out.append(
            _empty_keyframe(
                len(out),
                dur,
                "last",
                f.keyframe_last_prompt or "",
                f.keyframe_last_path,
            )
        )
    for i, kf in enumerate(out):
        kf["index"] = i
    return out


def _keyframes_list(f: StoryboardFrame) -> list[dict[str, Any]]:
    raw = getattr(f, "keyframes", None) or []
    if isinstance(raw, list) and raw:
        out = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "middle")
            if i == 0:
                role = "first"
            out.append(
                _empty_keyframe(
                    i,
                    float(item.get("t_sec") or 0.0),
                    role,
                    str(item.get("image_prompt") or item.get("prompt") or ""),
                    item.get("path"),
                )
            )
        if out:
            out[-1]["role"] = "last" if len(out) > 1 else out[-1]["role"]
            if len(out) == 1:
                out[0]["role"] = "first"
            return out
    return _legacy_keyframes_from_columns(f)


def _sync_legacy_keyframe_columns(
    f: StoryboardFrame, keyframes: list[dict[str, Any]]
) -> None:
    """Keep first/mid/last columns in sync for movie/continuity helpers."""
    f.keyframes = keyframes
    first = keyframes[0] if keyframes else None
    last = keyframes[-1] if keyframes else None
    middles = [k for k in keyframes if k.get("role") == "middle"]
    mid = middles[len(middles) // 2] if middles else None
    f.keyframe_first_path = (first or {}).get("path")
    f.keyframe_last_path = (last or {}).get("path")
    f.keyframe_mid_path = (mid or {}).get("path")
    f.keyframe_first_prompt = (first or {}).get("image_prompt") or ""
    f.keyframe_last_prompt = (last or {}).get("image_prompt") or ""
    f.keyframe_mid_prompt = (mid or {}).get("image_prompt") or ""


def _cast_ref_sheet_path(project_id: int, frame_id: int) -> str | None:
    """Path to the last composite cast contact sheet for this beat, if present."""
    settings = get_settings()
    path = (
        settings.media_dir
        / "projects"
        / str(project_id)
        / "frames"
        / str(frame_id)
        / "cast_ref_sheet.png"
    )
    return str(path) if path.is_file() else None


def generate_cast_ref_sheet(project_id: int, frame_id: int) -> dict[str, Any]:
    """Build (or refresh) the labeled cast/outfit contact sheet for a beat."""
    from app.services.characters import cast_reference_for_frame

    settings = get_settings()
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")

    media = (
        settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    )
    media.mkdir(parents=True, exist_ok=True)
    dest = media / "cast_ref_sheet.png"
    sheet = cast_reference_for_frame(project_id, frame_id, dest=dest)
    if sheet is None:
        raise ValueError(
            "no character/outfit reference images for this beat's cast — "
            "generate outfit or character refs first"
        )
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        assert f
        payload = _frame_dict(f)
    payload["cast_ref_sheet_path"] = str(sheet)
    return payload


def _frame_dict(f: StoryboardFrame) -> dict[str, Any]:
    keyframes = _keyframes_list(f)
    return {
        "id": f.id,
        "position": f.position,
        "description": f.description,
        "visual_prompt": f.visual_prompt,
        "still_path": f.still_path,
        "cast_ref_sheet_path": _cast_ref_sheet_path(f.project_id, f.id),
        "keyframes": keyframes,
        "keyframe_first_path": f.keyframe_first_path,
        "keyframe_mid_path": f.keyframe_mid_path,
        "keyframe_last_path": f.keyframe_last_path,
        "keyframe_first_prompt": f.keyframe_first_prompt or "",
        "keyframe_mid_prompt": f.keyframe_mid_prompt or "",
        "keyframe_last_prompt": f.keyframe_last_prompt or "",
        "preview_path": f.preview_path,
        "duration_hint_sec": f.duration_hint_sec,
        "is_new_shot": f.is_new_shot,
        "cast": list(getattr(f, "cast", None) or []),
    }


def _keyframes_ready(keyframes: list[dict[str, Any]]) -> bool:
    return bool(keyframes) and all((k.get("path") or "").strip() for k in keyframes)


async def rebuild_frame_keyframe_prompts(
    project_id: int, frame_id: int
) -> dict[str, Any]:
    """LLM-plan a variable keyframe series (≤2s spacing). Keeps existing paths when prompts only."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        description = f.description or ""
        visual = f.visual_prompt or f.description or ""
        duration = float(f.duration_hint_sec or 4.0)
        is_new = bool(f.is_new_shot)
        prev_last_prompt = None
        prev_last_path = None
        if not is_new and idx > 0:
            prev_kfs = _keyframes_list(frames[idx - 1])
            if prev_kfs:
                prev_last_prompt = prev_kfs[-1].get("image_prompt") or None
                prev_last_path = prev_kfs[-1].get("path") or None
        existing_paths = {
            i: (kf.get("path") or None) for i, kf in enumerate(_keyframes_list(f))
        }

    from app.services.characters import cast_sheet_for_frame

    cast_sheet = cast_sheet_for_frame(project_id, frame_id)
    share_first = bool(not is_new and prev_last_prompt)
    planned = await llm.plan_keyframe_series(
        description=description,
        visual=visual,
        duration_sec=duration,
        is_new_shot=is_new,
        prev_last_prompt=prev_last_prompt,
        cast_sheet=cast_sheet,
        share_first_from_prev=share_first,
    )
    keyframes = planned["keyframes"]
    for kf in keyframes:
        # Preserve rendered path if slot index still exists (best-effort)
        if kf["index"] in existing_paths and existing_paths[kf["index"]]:
            kf["path"] = existing_paths[kf["index"]]
    # Continuous beats: first keyframe is exactly the previous beat's last.
    if share_first and prev_last_path:
        keyframes[0]["path"] = prev_last_path
        keyframes[0]["image_prompt"] = prev_last_prompt

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        _sync_legacy_keyframe_columns(fr, keyframes)
        db.commit()
        db.refresh(fr)
        return _frame_dict(fr)


def _frames_payload(project_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        return [_frame_dict(f) for f in p.frames]


async def propose_storyboard(
    project_id: int, max_frames: int = 8
) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        story = p.story or p.premise
        if not story:
            raise ValueError("project has no story/premise")
        # replace frames
        for f in list(p.frames):
            db.delete(f)
        db.commit()

    from app.services import characters as char_svc

    # Ensure cast exists before proposing beats that name them.
    try:
        await char_svc.detect_characters(project_id, replace_auto=False)
    except Exception:
        pass
    cast_sheet = ""
    try:
        cast_sheet = char_svc.cast_sheet_for_project(project_id)
    except Exception:
        cast_sheet = ""

    proposed = await llm.propose_storyboard(
        story, max_frames=max_frames, cast_sheet=cast_sheet
    )
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        assert p is not None
        p.storyboard_approved = False
        for item in proposed:
            db.add(
                StoryboardFrame(
                    project_id=project_id,
                    position=item["position"],
                    description=item["description"],
                    visual_prompt=item["visual_prompt"],
                    duration_hint_sec=item["duration_hint_sec"],
                    is_new_shot=item["is_new_shot"],
                    keyframes=[],
                )
            )
        db.commit()
    try:
        await char_svc.sync_intro_frames(project_id)
    except Exception:
        pass
    # Plan prompts per frame (LLM). Failures leave empty keyframes for later rebuild.
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        assert p is not None
        frame_ids = [f.id for f in sorted(p.frames, key=lambda x: x.position)]
    for fid in frame_ids:
        try:
            await rebuild_frame_keyframe_prompts(project_id, fid)
        except Exception:
            continue
    return _frames_payload(project_id)


def update_frame(project_id: int, frame_id: int, **fields: Any) -> dict[str, Any]:
    allowed = {
        "description",
        "visual_prompt",
        "duration_hint_sec",
        "is_new_shot",
        "position",
        "still_path",
        "preview_path",
        "keyframe_first_prompt",
        "keyframe_mid_prompt",
        "keyframe_last_prompt",
        "keyframes",
        "cast",
    }
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "cast":
                if not isinstance(v, list):
                    raise ValueError("cast must be a list")
                normalized = []
                for item in v:
                    if not isinstance(item, dict):
                        continue
                    try:
                        cid = int(item.get("character_id"))
                    except (TypeError, ValueError):
                        continue
                    oid = item.get("outfit_id")
                    normalized.append(
                        {
                            "character_id": cid,
                            "outfit_id": str(oid) if oid else None,
                        }
                    )
                f.cast = normalized
            elif k == "keyframes":
                if not isinstance(v, list):
                    raise ValueError("keyframes must be a list")
                normalized = []
                for i, item in enumerate(v):
                    if not isinstance(item, dict):
                        continue
                    role = str(item.get("role") or "middle")
                    if i == 0:
                        role = "first"
                    normalized.append(
                        _empty_keyframe(
                            i,
                            float(item.get("t_sec") or 0.0),
                            role,
                            str(item.get("image_prompt") or item.get("prompt") or ""),
                            item.get("path"),
                        )
                    )
                if normalized:
                    normalized[-1]["role"] = (
                        "last" if len(normalized) > 1 else normalized[0]["role"]
                    )
                _sync_legacy_keyframe_columns(f, normalized)
            else:
                setattr(f, k, v)
        db.commit()
        db.refresh(f)
        return _frame_dict(f)


def delete_frame_media(project_id: int, frame_id: int, kind: str) -> dict[str, Any]:
    """Clear still, preview, keyframe:N, or legacy keyframe_* kind."""
    settings = get_settings()
    shared = False
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")

        old: str | None = None
        if kind in ("still", "preview"):
            path_attr = "still_path" if kind == "still" else "preview_path"
            old = getattr(f, path_attr)
            setattr(f, path_attr, None)
        elif kind.startswith("keyframe:"):
            idx = int(kind.split(":", 1)[1])
            keyframes = _keyframes_list(f)
            if idx < 0 or idx >= len(keyframes):
                raise ValueError(f"keyframe index {idx} out of range")
            old = keyframes[idx].get("path")
            keyframes[idx]["path"] = None
            _sync_legacy_keyframe_columns(f, keyframes)
        elif kind in ("keyframe_first", "keyframe_mid", "keyframe_last"):
            keyframes = _keyframes_list(f)
            role = {
                "keyframe_first": "first",
                "keyframe_mid": "middle",
                "keyframe_last": "last",
            }[kind]
            target = None
            if role == "first" and keyframes:
                target = 0
            elif role == "last" and keyframes:
                target = len(keyframes) - 1
            elif role == "middle":
                middles = [
                    i for i, k in enumerate(keyframes) if k.get("role") == "middle"
                ]
                target = middles[len(middles) // 2] if middles else None
            if target is None:
                raise ValueError(f"no {kind} on frame")
            old = keyframes[target].get("path")
            keyframes[target]["path"] = None
            _sync_legacy_keyframe_columns(f, keyframes)
        else:
            raise ValueError(
                "kind must be still, preview, keyframe:N, keyframe_first, "
                "keyframe_mid, or keyframe_last"
            )

        db.commit()
        db.refresh(f)
        payload = _frame_dict(f)
        payload["deleted"] = kind

        # Don't unlink files still referenced as another beat's shared keyframe.
        if old:
            proj = db.get(Project, project_id)
            for fr in proj.frames if proj else []:
                if fr.id == frame_id:
                    continue
                for kf in _keyframes_list(fr):
                    if (kf.get("path") or "") == old:
                        shared = True
                        break
                if shared:
                    break

    if old and not shared:
        try:
            p = Path(old)
            media_root = settings.media_dir.resolve()
            if p.is_file() and str(p.resolve()).startswith(str(media_root)):
                p.unlink()
        except OSError:
            pass
    return payload


def approve_storyboard(project_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        if not p.frames:
            raise ValueError("no storyboard frames")
        p.storyboard_approved = True
        db.commit()
        return {"id": p.id, "storyboard_approved": True}


def _truncate(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


_SCENE_LIST_RE = re.compile(r"(?i)\bscene\s*\d+\b")


def _is_scene_list(text: str) -> bool:
    """True when text enumerates multiple scenes (Flux draws these as grids)."""
    return len(_SCENE_LIST_RE.findall(text or "")) >= 2


def _world_lock(*, premise: str, story: str, cast_sheet: str = "") -> str:
    """Short cast/setting lock for image models — never a multi-scene script."""
    parts: list[str] = []
    cast = (cast_sheet or "").strip()
    if cast:
        parts.append(cast)
    premise_bit = _truncate(premise or "", 320)
    if premise_bit:
        parts.append(premise_bit)
    elif not cast:
        story_bit = (story or "").strip()
        if story_bit and not _is_scene_list(story_bit):
            parts.append(_truncate(story_bit, 320))
    return " ".join(parts)


def _frame_wants_on_screen_text(frame_prompt: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(text|title card|end card|credits|neon|screen|sign|caption|subtitle)\b|"
            r"[\"'].+[\"']",
            frame_prompt or "",
        )
    )


def still_negative_prompt(frame_prompt: str = "") -> str:
    base = (
        "blurry, watermark, logo, inconsistent characters, different person each frame, "
        "style change, collage, comic, manga, storyboard, panels, panel layout, grid, "
        "split screen, montage, multiple images, contact sheet, triptych, scrapbook, "
        "comic strip, multi-panel, 2x2, 3x2, tiled images, film strip, border frames"
    )
    if _frame_wants_on_screen_text(frame_prompt):
        return base
    return base + ", text overlay, on-screen text, title card, end card, neon sign text"


def build_visual_prompt(
    *,
    story: str,
    title: str,
    genre: str,
    frame_prompt: str,
    premise: str = "",
    cast_sheet: str = "",
    prev_prompt: str | None = None,
    next_prompt: str | None = None,
) -> str:
    """Compose an image prompt for ONE shot with light continuity lock.

    Do not dump multi-scene story scripts or neighbor beats into the prompt —
    Flux/Klein often renders those as a literal storyboard collage. Prefer the
    short premise + cast sheet as world lock; ignore prev/next scene text.
    """
    from app.services.llm import style_lock_phrase

    _ = title, prev_prompt, next_prompt  # title/neighbors unused for image models
    frame_bit = _truncate(frame_prompt or "", 400)
    world = _world_lock(premise=premise, story=story, cast_sheet=cast_sheet)
    style = style_lock_phrase(genre)
    parts: list[str] = [f"{style}."]
    if genre and genre.lower() not in style.lower():
        parts.append(f"{genre} genre.")
    if world:
        parts.append(f"Film continuity for: {world}.")
    parts.append(f"Show only this beat: {frame_bit}.")
    parts.append(
        "Same cast, wardrobe, and location look; do not show other story beats "
        "or a multi-panel layout."
    )
    return " ".join(parts)


async def generate_frame_visual(
    project_id: int,
    frame_id: int,
    *,
    kind: str = "still",
    workflow_id: str | None = None,
    num_frames: int = 33,
    video_backend: str | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    from app.services.characters import (
        cast_reference_for_frame,
        cast_sheet_for_frame,
        cast_sheet_for_project,
    )
    from app.services.video_backends import get_video_backend, normalize_backend_id

    try:
        cast_sheet = cast_sheet_for_frame(project_id, frame_id)
    except KeyError:
        cast_sheet = cast_sheet_for_project(project_id)
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frame_prompt = f.visual_prompt or f.description or ""
        genre = p.genre or ""
        prompt = build_visual_prompt(
            story=p.story or "",
            premise=p.premise or "",
            title=p.title or "",
            genre=genre,
            frame_prompt=frame_prompt,
            cast_sheet=cast_sheet,
        )
        project_backend = getattr(p, "video_backend", None)

    if kind == "still":
        comfy = ComfyUIClient()
        seed = frame_id * 17
        prefix = f"local_video/p{project_id}_f{frame_id}_still"
        neg = still_negative_prompt(frame_prompt)
        media = (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        media.mkdir(parents=True, exist_ok=True)
        from app.services.characters import (
            build_identity_pair_sheet,
            cast_entries_for_sheet,
            list_cast_reference_panels,
            prepare_single_ref_canvas,
        )
        from app.services.llm import (
            style_negatives,
            wardrobe_conflict_negatives,
            wardrobe_gap_negatives,
        )

        # UI contact sheet (labeled); generation uses iterative single-ID locks instead.
        try:
            cast_reference_for_frame(
                project_id, frame_id, dest=media / "cast_ref_sheet.png"
            )
        except KeyError:
            pass

        panels: list[tuple[str, Path, bool]] = []
        try:
            panels = list_cast_reference_panels(project_id, frame_id=frame_id)
        except KeyError:
            panels = []

        # Continuity base: keep faces from an existing still (or prior beat) instead of
        # re-rolling identity every regenerate — later shots follow whatever last still
        # invented if we always restage from outfit refs alone.
        continuity_base: Path | None = None
        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            p_row = db.get(Project, project_id)
            assert p_row
            frames = sorted(p_row.frames, key=lambda x: x.position)
            idx = next(i for i, x in enumerate(frames) if x.id == frame_id)
            is_new_shot = bool(fr.is_new_shot)
            if fr.still_path:
                try:
                    continuity_base = _resolve_media_file(str(fr.still_path))
                except (FileNotFoundError, ValueError, OSError):
                    continuity_base = None
            if continuity_base is None and not is_new_shot and idx > 0:
                prev_path = frames[idx - 1].still_path
                if prev_path:
                    try:
                        continuity_base = _resolve_media_file(str(prev_path))
                    except (FileNotFoundError, ValueError, OSError):
                        continuity_base = None
            if fresh:
                continuity_base = None
            # Inherit prior beat cast when this beat has none (continuous shots).
            selection = list(getattr(fr, "cast", None) or [])
            if (
                not selection
                and not is_new_shot
                and idx > 0
                and list(getattr(frames[idx - 1], "cast", None) or [])
            ):
                fr.cast = list(frames[idx - 1].cast or [])
                db.commit()
                try:
                    panels = list_cast_reference_panels(project_id, frame_id=frame_id)
                    cast_reference_for_frame(
                        project_id, frame_id, dest=media / "cast_ref_sheet.png"
                    )
                except KeyError:
                    pass

        wardrobe_neg = ""
        wardrobe_by_name: dict[str, str] = {}
        wardrobe_prompt_by_name: dict[str, str] = {}
        try:
            with SessionLocal() as db:
                fr = db.get(StoryboardFrame, frame_id)
                selection = list(getattr(fr, "cast", None) or []) if fr else []
            for e in cast_entries_for_sheet(
                project_id, cast_selection=selection if selection else None
            ):
                name = (e.get("name") or "").strip() or "Character"
                oname = (e.get("outfit_name") or "").strip()
                ward = (e.get("wardrobe_prompt") or "").strip()
                if ward:
                    wardrobe_prompt_by_name[name.lower()] = ward
                    wardrobe_by_name[name.lower()] = (
                        f"{name} / {oname}: {ward}" if oname else f"{name}: {ward}"
                    )
                wardrobe_neg = ", ".join(
                    x
                    for x in (
                        wardrobe_neg,
                        wardrobe_conflict_negatives(
                            e.get("appearance_prompt") or "",
                            ward,
                        ),
                        wardrobe_gap_negatives(ward),
                    )
                    if x
                )
        except KeyError:
            pass
        style_neg = style_negatives(genre)
        extra_neg = ", ".join(x for x in (wardrobe_neg, style_neg) if x)
        if extra_neg:
            neg = f"{neg}, {extra_neg}"
        neg = (
            neg
            + ", collage, contact sheet, multi-panel, split screen, grid layout, "
            "side by side panels"
        )

        if panels:
            # Flux ReferenceLatent is single-identity: lock cast one at a time.
            # Pass 0 places ONLY the first cast member; later passes REPLACE — never ADD.
            cast_names = [
                (lab.split("/")[0].strip() or lab).strip() for lab, _p, _a in panels
            ]
            cast_count = len(cast_names)
            cast_list = ", ".join(cast_names)
            current: Path | None = None
            prompt_id = ""
            neg = (
                neg
                + ", extra person, duplicate character, additional character, crowd, "
                "wrong cast size"
            )
            for i, (label, path, _approved) in enumerate(panels):
                name = (label.split("/")[0].strip() or label).strip()
                ward_bit = wardrobe_by_name.get(name.lower(), "")
                ward_exact = wardrobe_prompt_by_name.get(name.lower(), "")
                ward_must = (
                    f"{name} MUST wear exactly the outfit shown on the RIGHT panel: "
                    f"{ward_exact}. Match RIGHT clothing; do not invent garments or "
                    "accessories that are not on the RIGHT. "
                    if ward_exact
                    else f"Match {name}'s clothing exactly to the RIGHT panel. "
                )
                others = [n for n in cast_names if n.lower() != name.lower()]
                others_bit = (
                    f"Keep {', '.join(others)} from LEFT unchanged. "
                    if others and i > 0
                    else ""
                )
                beat_bit = _truncate(frame_prompt, 280)
                if i == 0 and cast_count > 1 and continuity_base is None:
                    # Avoid the beat naming other cast members so Flux doesn't invent them.
                    beat_bit = (
                        f"{name} alone in this beat setting. "
                        f"Context: {_truncate(frame_prompt, 200)}"
                    )
                if i == 0 and continuity_base is not None:
                    ref = build_identity_pair_sheet(
                        continuity_base, path, media / "cast_lock_0.png"
                    )
                    instruction = (
                        "LEFT half is the continuity still. RIGHT half is the "
                        f"ground-truth look for {label}. "
                        f"REPLACE {name} so face, hair, eyes, proportions, art style, "
                        f"AND wardrobe match the RIGHT panel exactly. {ward_must}"
                        f"Final shot must contain exactly {cast_count} people: {cast_list}. "
                        "Remove anyone not in that cast list. "
                        f"{others_bit}"
                        "ONE continuous shot — not a split screen. "
                        + (f"Wardrobe note: {ward_bit}. " if ward_bit else "")
                        + f"Scene beat: {beat_bit}"
                    )
                elif i == 0:
                    ref = prepare_single_ref_canvas(
                        path, media / "cast_lock_0.png"
                    )
                    only = (
                        f"Show ONLY {name} in this shot — no other cast members yet. "
                        if cast_count > 1
                        else ""
                    )
                    instruction = (
                        f"Restage this exact character ({label}) into the beat. "
                        f"{only}"
                        "Keep face, eye color, hair, proportions, and art style identical "
                        "to the reference. "
                        f"{ward_must}"
                        + (f"Wardrobe note: {ward_bit}. " if ward_bit else "")
                        + f"Scene beat: {beat_bit}"
                    )
                else:
                    assert current is not None
                    ref = build_identity_pair_sheet(
                        current, path, media / f"cast_lock_{i}.png"
                    )
                    instruction = (
                        "LEFT half is the current scene. RIGHT half is the ground-truth "
                        f"look for {label}. REPLACE any wrong stand-in for {name} with "
                        f"{name} matching the RIGHT panel exactly — same face, eyes, hair, "
                        "proportions, art style, and wardrobe. "
                        f"Do NOT add an extra person. {ward_must}"
                        f"Final shot must contain exactly {cast_count} people: {cast_list}. "
                        "Remove duplicates and anyone not on that list. "
                        f"{others_bit}"
                        "ONE continuous shot — not a split screen. "
                        + (f"Wardrobe note: {ward_bit}. " if ward_bit else "")
                        + f"Scene beat: {beat_bit}"
                    )
                uploaded = await comfy.upload_image(ref)
                edit_prompt = build_edit_prompt(
                    instruction=instruction,
                    frame_prompt=frame_prompt,
                    cast_sheet=cast_sheet,
                    genre=genre,
                    from_cast_sheet=True,
                )
                graph = apply_params(
                    "still_edit",
                    {
                        "positive_prompt": edit_prompt,
                        "negative_prompt": neg,
                        "seed": seed + i * 17,
                        "filename_prefix": f"{prefix}_p{i}",
                        "width": 1024,
                        "height": 576,
                        "steps": 30,
                        "cfg": 5.5,
                    },
                    uploaded_image_name=uploaded,
                )
                prompt_id = await comfy.queue_prompt(graph)
                history = await comfy.wait_for_prompt(prompt_id)
                outs = comfy.collect_outputs(history)
                if not outs:
                    raise RuntimeError(
                        f"ComfyUI produced no outputs for cast lock pass {i} ({label})"
                    )
                out = outs[0]
                dest = media / f"still_pass_{i}_{out['filename']}"
                await comfy.download_view(
                    out["filename"], dest, out["subfolder"], out["type"]
                )
                current = dest

            assert current is not None
            with SessionLocal() as db:
                fr = db.get(StoryboardFrame, frame_id)
                assert fr
                old = fr.still_path
                fr.still_path = str(current)
                db.commit()
            if old:
                try:
                    prev = _resolve_media_file(str(old))
                    if prev.resolve() != current.resolve() and prev.is_file():
                        prev.unlink()
                except (FileNotFoundError, ValueError, OSError):
                    pass
            return {
                "frame_id": frame_id,
                "kind": kind,
                "still_path": str(current),
                "preview_path": None,
                "prompt_id": prompt_id,
                "cast_lock_passes": len(panels),
            }

        workflow_id = workflow_id or "still_hero"
        graph = apply_params(
            workflow_id,
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
    else:
        backend = get_video_backend(
            normalize_backend_id(
                video_backend
                or project_backend
                or settings.default_video_backend
                or "wan"
            )
        )
        validate_frame_count(num_frames)
        media = (
            settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        )
        media.mkdir(parents=True, exist_ok=True)
        dest = await backend.render_t2v(
            project_id=project_id,
            frame_id=frame_id,
            prompt=prompt,
            label="preview",
            num_frames=num_frames,
            seed=frame_id * 17,
            dest_dir=media,
            filename_prefix=f"local_video/p{project_id}_f{frame_id}_preview",
            negative_prompt=(
                "blurry, watermark, text, static, inconsistent characters, style change, "
                "collage, comic, storyboard, panels, grid, split screen, montage, "
                "multi-panel, contact sheet"
            ),
        )
        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            had_still = bool(fr.still_path)
            fr.preview_path = str(dest)
            still_out = fr.still_path
            if not had_still:
                frames_dir = media / "extracted"
                frames = extract_frames_from_video(dest, frames_dir)
                if frames:
                    still = media / "still_from_preview.png"
                    still.write_bytes(frames[0].read_bytes())
                    fr.still_path = str(still)
                    still_out = str(still)
            db.commit()
        return {
            "frame_id": frame_id,
            "kind": kind,
            "preview_path": str(dest),
            "still_path": still_out,
            "video_backend": backend.id,
        }

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    saved_path = None
    for out in outputs:
        dest = media / out["filename"]
        await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
        saved_path = dest
        # Preview clips: only seed a still from the first frame when none exists yet.
        if out["kind"] in ("gifs", "videos") or dest.suffix.lower() in {
            ".mp4",
            ".webm",
            ".gif",
        }:
            with SessionLocal() as db:
                fr = db.get(StoryboardFrame, frame_id)
                assert fr
                had_still = bool(fr.still_path)
                fr.preview_path = str(dest)
                still_out = fr.still_path
                if not had_still:
                    frames_dir = media / "extracted"
                    frames = extract_frames_from_video(dest, frames_dir)
                    if frames:
                        still = media / "still_from_preview.png"
                        still.write_bytes(frames[0].read_bytes())
                        fr.still_path = str(still)
                        still_out = str(still)
                db.commit()
            return {
                "frame_id": frame_id,
                "kind": kind,
                "preview_path": str(dest),
                "still_path": still_out,
                "prompt_id": prompt_id,
            }

    if saved_path:
        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            if kind == "still":
                fr.still_path = str(saved_path)
            else:
                fr.preview_path = str(saved_path)
            db.commit()
        return {
            "frame_id": frame_id,
            "kind": kind,
            "still_path": str(saved_path) if kind == "still" else None,
            "preview_path": str(saved_path) if kind != "still" else None,
            "prompt_id": prompt_id,
        }
    raise RuntimeError("ComfyUI produced no outputs")


def _resolve_media_file(stored: str) -> Path:
    """Resolve a DB media path (/media/...) to a local file under MEDIA_DIR."""
    settings = get_settings()
    raw = (stored or "").strip()
    if not raw:
        raise FileNotFoundError("empty media path")
    direct = Path(raw)
    if direct.is_file():
        return direct
    rel = raw
    for marker in ("/media/", "media/"):
        idx = raw.find(marker)
        if idx >= 0:
            rel = raw[idx + len(marker) :]
            break
    candidate = (settings.media_dir / rel).resolve()
    media_root = settings.media_dir.resolve()
    if not str(candidate).startswith(str(media_root)):
        raise ValueError("media path escapes MEDIA_DIR")
    if not candidate.is_file():
        raise FileNotFoundError(f"media file not found: {stored}")
    return candidate


def build_edit_prompt(
    *,
    instruction: str,
    frame_prompt: str = "",
    cast_sheet: str = "",
    genre: str = "",
    from_cast_sheet: bool = False,
) -> str:
    from app.services.llm import style_lock_phrase

    instr = _truncate((instruction or "").strip(), 900)
    if not instr:
        raise ValueError("edit instruction is required")
    beat = _truncate(frame_prompt or "", 220)
    parts = [
        f"Edit this image into {style_lock_phrase(genre)}.",
        f"Instruction: {instr}.",
    ]
    if from_cast_sheet:
        parts.append(
            "CRITICAL identity lock: each person must keep the same face, eye shape, "
            "hair, age, body proportions, and art style as their labeled contact-sheet "
            "panel. Do not turn stylized/puppet characters into live-action people."
        )
    parts.append(
        "Preserve character identity. Match wardrobe to the cast lock / contact-sheet "
        "panels when provided — do not keep a different outfit from story titles, "
        "premises, or old appearance notes."
    )
    parts.append(
        "Output one continuous camera shot only — no collage, panels, or grid."
    )
    cast = (cast_sheet or "").strip()
    if cast:
        parts.append(f"{cast}")
        parts.append(
            "Faces follow the cast lock / panels; wardrobe in the cast lock is mandatory."
        )
    if beat:
        parts.append(f"Original beat context: {beat}.")
    return " ".join(parts)


def compose_keyframe_prompt(
    prompt: str, *, cast_sheet: str = "", genre: str = ""
) -> str:
    """Inject cast lock into a keyframe image prompt at Comfy render time."""
    body = (prompt or "").strip()
    if not body:
        raise ValueError("keyframe prompt is required")
    parts: list[str] = []
    if genre:
        parts.append(f"{genre} genre.")
    cast = (cast_sheet or "").strip()
    if cast:
        parts.append(cast)
        parts.append("Match these exact character looks.")
    parts.append(body)
    if cast:
        parts.append(
            "Same named people and wardrobe as the cast lock; do not invent look changes."
        )
    return " ".join(parts)


def merge_keyframe_prompt_with_edit(existing_prompt: str, instruction: str) -> str:
    """Keep keyframe generation prompt aligned with cumulative edit intent."""
    base = (existing_prompt or "").strip()
    instr = (instruction or "").strip()
    if not instr:
        raise ValueError("edit instruction is required")
    if not base:
        return instr

    lower_base = base.lower()
    lower_instr = instr.lower()
    if lower_instr in lower_base:
        return base

    marker = "Edit adjustments:"
    if marker in base:
        return f"{base}\n- {instr}"
    return f"{base}\n\n{marker}\n- {instr}"


async def edit_frame_still(
    project_id: int,
    frame_id: int,
    *,
    instruction: str,
    workflow_id: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Prompt-edit an existing still (Flux ReferenceLatent), replacing the still file."""
    settings = get_settings()
    from app.services.characters import cast_sheet_for_frame, cast_sheet_for_project

    try:
        cast_sheet = cast_sheet_for_frame(project_id, frame_id)
    except KeyError:
        cast_sheet = cast_sheet_for_project(project_id)
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        if not f.still_path:
            raise ValueError("frame has no still to edit — generate one first")
        still_stored = f.still_path
        frame_prompt = f.visual_prompt or f.description or ""

    source = _resolve_media_file(still_stored)
    prompt = build_edit_prompt(
        instruction=instruction,
        frame_prompt=frame_prompt,
        cast_sheet=cast_sheet,
    )
    workflow_id = workflow_id or "still_edit"
    comfy = ComfyUIClient()
    uploaded = await comfy.upload_image(source)
    params = {
        "positive_prompt": prompt,
        "negative_prompt": still_negative_prompt(frame_prompt),
        "seed": seed if seed is not None else (frame_id * 17 + 91),
        "filename_prefix": f"local_video/p{project_id}_f{frame_id}_edit",
        "width": 1024,
        "height": 576,
        "steps": 20,
        "cfg": 5.0,
    }
    graph = apply_params(workflow_id, params, uploaded_image_name=uploaded)
    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError("ComfyUI produced no outputs")

    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])

    # Drop previous still file when it is a different path under media_dir.
    try:
        old = _resolve_media_file(still_stored)
        if old.resolve() != dest.resolve() and old.is_file():
            old.unlink()
    except (FileNotFoundError, ValueError, OSError):
        pass

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        fr.still_path = str(dest)
        db.commit()

    return {
        "frame_id": frame_id,
        "kind": "still_edit",
        "still_path": str(dest),
        "instruction": instruction,
        "prompt_id": prompt_id,
    }


async def generate_all_stills(
    project_id: int,
    *,
    workflow_id: str | None = None,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Generate a still for every storyboard frame missing one (sequential)."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {"id": f.id, "still_path": f.still_path}
            for f in sorted(p.frames, key=lambda x: x.position)
        ]
    if not frames:
        raise ValueError("no storyboard frames")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fr in frames:
        if skip_existing and fr.get("still_path"):
            results.append(
                {"frame_id": fr["id"], "skipped": True, "still_path": fr["still_path"]}
            )
            continue
        try:
            out = await generate_frame_visual(
                project_id,
                fr["id"],
                kind="still",
                workflow_id=workflow_id,
            )
            results.append(out)
        except Exception as e:
            errors.append({"frame_id": fr["id"], "error": str(e)})

    return {
        "project_id": project_id,
        "generated": len([r for r in results if not r.get("skipped")]),
        "skipped": len([r for r in results if r.get("skipped")]),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


def build_transition_prompt(
    *,
    premise: str,
    start_prompt: str,
    end_prompt: str,
) -> str:
    """Prompt for a clip that starts on one still and moves toward the next."""
    world = _truncate(premise or "", 280)
    start = _truncate(start_prompt or "", 220)
    end = _truncate(end_prompt or "", 220)
    parts = [
        "Cinematic continuous video, one camera move, smooth motion between two keyframes.",
        "Begin matched to the starting image; progress toward the ending beat.",
    ]
    if world:
        parts.append(f"Film continuity for: {world}.")
    if start:
        parts.append(f"Starting beat: {start}.")
    if end:
        parts.append(f"Move toward: {end}.")
    parts.append(
        "Keep the same cast, wardrobe, and location; do not jump-cut or show a collage."
    )
    return " ".join(parts)


async def generate_between_stills(
    project_id: int,
    frame_id: int,
    *,
    workflow_id: str | None = None,
    num_frames: int = 33,
    video_backend: str | None = None,
) -> dict[str, Any]:
    """Bridge this beat's end image into the next beat's start via FLF2V."""
    validate_frame_count(num_frames)
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next((i for i, fr in enumerate(frames) if fr.id == frame_id), None)
        if idx is None:
            raise KeyError(f"frame {frame_id} not found")
        if idx + 1 >= len(frames):
            raise ValueError("last frame has no next still to transition toward")
        cur = frames[idx]
        nxt = frames[idx + 1]
        cur_kfs = _keyframes_list(cur)
        nxt_kfs = _keyframes_list(nxt)
        # Prefer step keyframes when present: last of this step → first of next.
        start_stored = (
            (cur_kfs[-1].get("path") if cur_kfs else None)
            or cur.keyframe_last_path
            or cur.still_path
        )
        end_ref = (
            (nxt_kfs[0].get("path") if nxt_kfs else None)
            or nxt.keyframe_first_path
            or nxt.still_path
        )
        if not start_stored:
            raise ValueError(
                "current frame has no last keyframe or still — create keyframes/stills first"
            )
        if not end_ref:
            raise ValueError(
                "next frame has no first keyframe or still — create keyframes/stills first"
            )
        # Continuous beats share the exact same boundary keyframe — no FLF bridge.
        if (not bool(nxt.is_new_shot)) and start_stored == end_ref:
            return {
                "frame_id": frame_id,
                "kind": "between",
                "skipped": True,
                "reason": "shared_boundary_keyframe",
                "start_path": start_stored,
                "end_path": end_ref,
                "next_frame_id": nxt.id,
            }
        start_beat = (
            (cur_kfs[-1].get("image_prompt") if cur_kfs else None)
            or cur.visual_prompt
            or cur.description
            or ""
        )
        end_beat = (
            (nxt_kfs[0].get("image_prompt") if nxt_kfs else None)
            or nxt.visual_prompt
            or nxt.description
            or ""
        )
        premise = p.premise or ""
        next_frame_id = nxt.id

    dest = await _bridge_clip_between_images(
        project_id=project_id,
        frame_id=frame_id,
        start_image=_resolve_media_file(start_stored),
        end_image=_resolve_media_file(end_ref),
        prompt=build_transition_prompt(
            premise=premise, start_prompt=start_beat, end_prompt=end_beat
        ),
        label="between",
        num_frames=num_frames,
        seed=frame_id * 17 + 3,
        workflow_id=workflow_id,
        video_backend=video_backend,
    )

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        # Never overwrite the still — only the bridging preview clip.
        fr.preview_path = str(dest)
        db.commit()

    return {
        "frame_id": frame_id,
        "next_frame_id": next_frame_id,
        "kind": "between_stills",
        "preview_path": str(dest),
        "workflow_id": workflow_id,
        "video_backend": video_backend,
    }


async def generate_all_between_stills(
    project_id: int,
    *,
    workflow_id: str | None = None,
    skip_existing: bool = True,
    num_frames: int = 33,
    video_backend: str | None = None,
) -> dict[str, Any]:
    """Generate between-stills clips for every consecutive still pair."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {
                "id": f.id,
                "still_path": f.still_path,
                "preview_path": f.preview_path,
                "keyframe_last_path": f.keyframe_last_path,
                "keyframe_first_path": f.keyframe_first_path,
            }
            for f in sorted(p.frames, key=lambda x: x.position)
        ]
    if len(frames) < 2:
        raise ValueError("need at least two storyboard frames")

    pairs = []
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        a_ok = a.get("keyframe_last_path") or a.get("still_path")
        b_ok = b.get("keyframe_first_path") or b.get("still_path")
        if a_ok and b_ok:
            pairs.append(a)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    for fr in pairs:
        if skip_existing and fr.get("preview_path"):
            results.append(
                {
                    "frame_id": fr["id"],
                    "skipped": True,
                    "preview_path": fr["preview_path"],
                }
            )
            skipped += 1
            continue
        try:
            out = await generate_between_stills(
                project_id,
                fr["id"],
                workflow_id=workflow_id,
                num_frames=num_frames,
                video_backend=video_backend,
            )
            results.append(out)
        except Exception as e:
            errors.append({"frame_id": fr["id"], "error": str(e)})

    return {
        "project_id": project_id,
        "generated": len([r for r in results if not r.get("skipped")]),
        "skipped": skipped,
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


async def _render_keyframe_image(
    *,
    project_id: int,
    frame_id: int,
    index: int,
    role: str,
    prompt: str,
    source_path: str | Path | None,
    seed: int,
    force_edit: bool = False,
) -> Path:
    """T2I when no source; otherwise edit-from-previous (preferred for continuity).

    Always reinjects the project cast sheet into the Comfy prompt (LLM planning alone
    is not enough). When generating a fresh first frame with no prior keyframe, prefer
    editing from a matching character reference still if one exists.
    """
    from app.services.characters import (
        cast_reference_for_frame,
        cast_sheet_for_frame,
    )

    settings = get_settings()
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)
    comfy = ComfyUIClient()
    try:
        cast_sheet = cast_sheet_for_frame(project_id, frame_id)
    except KeyError:
        from app.services.characters import cast_sheet_for_project

        cast_sheet = cast_sheet_for_project(project_id)
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        genre = (p.genre or "") if p else ""
    composed = compose_keyframe_prompt(prompt, cast_sheet=cast_sheet, genre=genre)
    neg = still_negative_prompt(prompt)
    label = f"{index:02d}_{role}"

    if source_path:
        src = _resolve_media_file(str(source_path))
        uploaded = await comfy.upload_image(src)
        edit_prompt = build_edit_prompt(
            instruction=prompt,
            frame_prompt=prompt,
            cast_sheet=cast_sheet,
        )
        graph = apply_params(
            "still_edit",
            {
                "positive_prompt": edit_prompt,
                "negative_prompt": neg,
                "seed": seed,
                "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{label}",
                "width": 1024,
                "height": 576,
                "steps": 20,
                "cfg": 5.0,
            },
            uploaded_image_name=uploaded,
        )
    else:
        if force_edit:
            raise ValueError("edit source required")
        cast_ref = cast_reference_for_frame(
            project_id,
            frame_id,
            dest=media / f"cast_ref_sheet_kf_{label}.png",
        )
        if cast_ref is not None:
            uploaded = await comfy.upload_image(cast_ref)
            from app.services.characters import cast_entries_for_sheet
            from app.services.llm import wardrobe_conflict_negatives

            wardrobe_neg = ""
            panel_lines: list[str] = []
            try:
                with SessionLocal() as db:
                    fr = db.get(StoryboardFrame, frame_id)
                    selection = list(getattr(fr, "cast", None) or []) if fr else []
                for e in cast_entries_for_sheet(
                    project_id, cast_selection=selection if selection else None
                ):
                    name = (e.get("name") or "").strip() or "Character"
                    oname = (e.get("outfit_name") or "").strip()
                    ward = (e.get("wardrobe_prompt") or "").strip()
                    if ward:
                        label = f"{name} / {oname}" if oname else name
                        panel_lines.append(f"{label}: {ward}")
                    wardrobe_neg = ", ".join(
                        x
                        for x in (
                            wardrobe_neg,
                            wardrobe_conflict_negatives(
                                e.get("appearance_prompt") or "",
                                ward,
                            ),
                        )
                        if x
                    )
            except KeyError:
                pass
            if wardrobe_neg:
                neg = f"{neg}, {wardrobe_neg}"
            wardrobe_block = (
                " Required clothing from each labeled panel — match exactly: "
                + "; ".join(panel_lines)
                + "."
                if panel_lines
                else ""
            )
            edit_prompt = build_edit_prompt(
                instruction=(
                    "The reference is a cast contact sheet: each labeled panel is the "
                    "ground-truth look for one character (face, hair, body, art style, "
                    "and this beat's wardrobe). Restage those SAME characters into ONE "
                    "continuous shot matching the instruction — keep faces and stylized "
                    "look from the panels."
                    f"{wardrobe_block} Instruction: {prompt}"
                ),
                frame_prompt=prompt,
                cast_sheet=cast_sheet,
                genre=genre,
                from_cast_sheet=True,
            )
            from app.services.llm import style_negatives

            style_neg = style_negatives(genre)
            kf_neg = neg
            if style_neg:
                kf_neg = f"{kf_neg}, {style_neg}"
            graph = apply_params(
                "still_edit",
                {
                    "positive_prompt": edit_prompt,
                    "negative_prompt": kf_neg
                    + ", collage, contact sheet, multi-panel, split screen, grid layout",
                    "seed": seed,
                    "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{label}",
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
                    "positive_prompt": composed,
                    "negative_prompt": neg,
                    "seed": seed,
                    "filename_prefix": f"local_video/p{project_id}_f{frame_id}_kf_{label}",
                },
            )

    prompt_id = await comfy.queue_prompt(graph)
    history = await comfy.wait_for_prompt(prompt_id)
    outputs = comfy.collect_outputs(history)
    if not outputs:
        raise RuntimeError(f"ComfyUI produced no outputs for keyframe {label}")
    out = outputs[0]
    dest = media / f"keyframe_{label}_{out['filename']}"
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


async def generate_frame_keyframes(
    project_id: int,
    frame_id: int,
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Create a variable keyframe series: edit-chain; new shot starts fresh T2I."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        is_new = bool(f.is_new_shot)
        keyframes = _keyframes_list(f)
        prev_last_path = None
        if not is_new and idx > 0:
            prev_kfs = _keyframes_list(frames[idx - 1])
            if prev_kfs:
                prev_last_path = prev_kfs[-1].get("path")

    if not keyframes or not all(
        (k.get("image_prompt") or "").strip() for k in keyframes
    ):
        await rebuild_frame_keyframe_prompts(project_id, frame_id)
        with SessionLocal() as db:
            f = db.get(StoryboardFrame, frame_id)
            assert f
            keyframes = _keyframes_list(f)

    if skip_existing and _keyframes_ready(keyframes):
        return {
            "frame_id": frame_id,
            "kind": "keyframes",
            "generated": [],
            "skipped": [k["role"] for k in keyframes],
            "keyframes": keyframes,
            "keyframe_first_path": keyframes[0].get("path") if keyframes else None,
            "keyframe_last_path": keyframes[-1].get("path") if keyframes else None,
        }

    generated: list[int] = []
    skipped: list[int] = []
    last_path: str | None = None

    for i, kf in enumerate(keyframes):
        # Continuous beat: first keyframe IS the previous beat's last image (shared path).
        if i == 0 and not is_new and prev_last_path:
            keyframes[0]["path"] = prev_last_path
            if not (keyframes[0].get("image_prompt") or "").strip():
                # Prefer prior last prompt when available from the shared image.
                pass
            skipped.append(0)
            last_path = prev_last_path
            with SessionLocal() as db:
                fr = db.get(StoryboardFrame, frame_id)
                assert fr
                _sync_legacy_keyframe_columns(fr, keyframes)
                db.commit()
            continue

        if skip_existing and (kf.get("path") or "").strip():
            skipped.append(i)
            last_path = kf.get("path")
            continue

        prompt = (kf.get("image_prompt") or "").strip()
        if not prompt:
            raise ValueError(f"frame {frame_id} keyframe {i} has no image_prompt")

        if i == 0:
            source = None  # new shot → T2I
        else:
            if not last_path:
                raise ValueError(
                    f"frame {frame_id} keyframe {i} needs previous keyframe image"
                )
            source = last_path

        dest = await _render_keyframe_image(
            project_id=project_id,
            frame_id=frame_id,
            index=i,
            role=str(kf.get("role") or "middle"),
            prompt=prompt,
            source_path=source,
            seed=frame_id * 31 + i * 97,
        )
        keyframes[i]["path"] = str(dest)
        last_path = str(dest)
        generated.append(i)

        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            _sync_legacy_keyframe_columns(fr, keyframes)
            db.commit()

    return {
        "frame_id": frame_id,
        "kind": "keyframes",
        "generated": generated,
        "skipped": skipped,
        "keyframes": keyframes,
        "keyframe_first_path": keyframes[0].get("path") if keyframes else None,
        "keyframe_last_path": keyframes[-1].get("path") if keyframes else None,
        "keyframe_mid_path": next(
            (k.get("path") for k in keyframes if k.get("role") == "middle"), None
        ),
    }


def _resolve_keyframe_index(
    keyframes: list[dict[str, Any]], phase_or_index: str | int
) -> int:
    if isinstance(phase_or_index, int) or str(phase_or_index).isdigit():
        idx = int(phase_or_index)
        if idx < 0 or idx >= len(keyframes):
            raise ValueError(f"keyframe index {idx} out of range")
        return idx
    phase = str(phase_or_index)
    if phase in ("first", "start"):
        return 0
    if phase in ("last", "end"):
        return len(keyframes) - 1
    if phase in ("mid", "middle"):
        middles = [i for i, k in enumerate(keyframes) if k.get("role") == "middle"]
        if not middles:
            raise ValueError("no middle keyframe")
        return middles[len(middles) // 2]
    raise ValueError("phase must be first, mid, last, or a keyframe index")


async def generate_one_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Render one keyframe slot; edits from previous in-series image when available."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        frames = sorted(p.frames, key=lambda x: x.position)
        idx = next(i for i, fr in enumerate(frames) if fr.id == frame_id)
        is_new = bool(f.is_new_shot)
        keyframes = _keyframes_list(f)
        if not keyframes:
            raise ValueError("no keyframe series — rebuild prompts first")
        ki = _resolve_keyframe_index(keyframes, phase)
        prompt = (keyframes[ki].get("image_prompt") or "").strip()
        old_path = keyframes[ki].get("path")
        prev_path = keyframes[ki - 1].get("path") if ki > 0 else None
        prev_shot_last = None
        if ki == 0 and not is_new and idx > 0:
            prev_kfs = _keyframes_list(frames[idx - 1])
            if prev_kfs:
                prev_shot_last = prev_kfs[-1].get("path")

    if not prompt:
        raise ValueError(f"frame {frame_id} keyframe {ki} has no image_prompt")

    # Continuous beat start: share previous beat's last keyframe exactly (no re-render).
    if ki == 0 and not is_new and prev_shot_last:
        keyframes[0]["path"] = prev_shot_last
        with SessionLocal() as db:
            fr = db.get(StoryboardFrame, frame_id)
            assert fr
            _sync_legacy_keyframe_columns(fr, keyframes)
            db.commit()
            db.refresh(fr)
            return _frame_dict(fr)

    if ki == 0:
        source = None
    else:
        source = prev_path
        if not source:
            raise ValueError("generate earlier keyframes first (edit chain)")

    dest = await _render_keyframe_image(
        project_id=project_id,
        frame_id=frame_id,
        index=ki,
        role=str(keyframes[ki].get("role") or "middle"),
        prompt=prompt,
        source_path=source,
        seed=seed if seed is not None else (frame_id * 31 + ki * 97),
    )
    if old_path and old_path != prev_shot_last:
        try:
            old = _resolve_media_file(old_path)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    keyframes[ki]["path"] = str(dest)
    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        _sync_legacy_keyframe_columns(fr, keyframes)
        db.commit()
        db.refresh(fr)
        return _frame_dict(fr)


async def edit_frame_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    *,
    instruction: str,
    seed: int | None = None,
) -> dict[str, Any]:
    """Prompt-edit an existing keyframe (or previous/still fallback) into that slot."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        keyframes = _keyframes_list(f)
        if not keyframes:
            raise ValueError("no keyframe series — rebuild prompts first")
        ki = _resolve_keyframe_index(keyframes, phase)
        keyframe_stored = keyframes[ki].get("path")
        prev_path = keyframes[ki - 1].get("path") if ki > 0 else None
        source_stored = keyframe_stored or prev_path or f.still_path
        if not source_stored:
            raise ValueError(
                f"frame has no keyframe {ki} or still to edit — generate one first"
            )
        effective_prompt = merge_keyframe_prompt_with_edit(
            str(keyframes[ki].get("image_prompt") or ""),
            instruction,
        )

    dest = await _render_keyframe_image(
        project_id=project_id,
        frame_id=frame_id,
        index=ki,
        role=str(keyframes[ki].get("role") or "middle"),
        prompt=effective_prompt,
        source_path=source_stored,
        seed=seed if seed is not None else (frame_id * 31 + 53 + ki),
        force_edit=True,
    )
    if keyframe_stored:
        try:
            old = _resolve_media_file(keyframe_stored)
            if old.resolve() != dest.resolve() and old.is_file():
                old.unlink()
        except (FileNotFoundError, ValueError, OSError):
            pass

    keyframes[ki]["path"] = str(dest)
    keyframes[ki]["image_prompt"] = effective_prompt
    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        _sync_legacy_keyframe_columns(fr, keyframes)
        db.commit()
        db.refresh(fr)
        return _frame_dict(fr)


async def generate_all_keyframes(
    project_id: int,
    *,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Generate first/mid/last keyframes for every storyboard frame."""
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frame_ids = [f.id for f in sorted(p.frames, key=lambda x: x.position)]
    if not frame_ids:
        raise ValueError("no storyboard frames")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for fid in frame_ids:
        try:
            results.append(
                await generate_frame_keyframes(
                    project_id, fid, skip_existing=skip_existing
                )
            )
        except Exception as e:
            errors.append({"frame_id": fid, "error": str(e)})

    return {
        "project_id": project_id,
        "frames": len(frame_ids),
        "completed": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


async def _bridge_clip_between_images(
    *,
    project_id: int,
    frame_id: int,
    start_image: Path,
    end_image: Path | None = None,
    prompt: str,
    label: str,
    num_frames: int,
    seed: int,
    workflow_id: str | None = None,
    video_backend: str | None = None,
) -> Path:
    """Generate a clip locked to start (and optionally end) image via FLF2V or I2V."""
    from app.db.models import Project
    from app.services.video_backends import get_video_backend, normalize_backend_id

    settings = get_settings()
    if video_backend is None:
        with SessionLocal() as db:
            p = db.get(Project, project_id)
            video_backend = (
                (getattr(p, "video_backend", None) if p else None)
                or settings.default_video_backend
                or "wan"
            )
    backend = get_video_backend(normalize_backend_id(video_backend))
    wfs = backend.workflows()

    # Explicit legacy / override workflow id (non-FLF maps).
    if workflow_id and workflow_id not in (
        None,
        "",
        "wan22_flf2v",
        "ltx_flf2v",
        wfs["flf2v"],
        wfs["i2v"],
        "wan22_i2v",
        "ltx_i2v",
    ):
        validate_frame_count(num_frames)
        comfy = ComfyUIClient()
        start_name = await comfy.upload_image(start_image)
        uploads: dict[str, str] = {"start_image": start_name}
        if end_image is not None:
            uploads["end_image"] = await comfy.upload_image(end_image)
        params = {
            "positive_prompt": prompt,
            "negative_prompt": (
                "blurry, watermark, text, static, jump cut, morphing face, flickering, "
                "collage, comic, storyboard, panels, grid, split screen, montage"
            ),
            "seed": seed,
            "num_frames": num_frames,
            "width": settings.default_width,
            "height": settings.default_height,
            "fps": settings.default_fps,
            "cfg": settings.default_cfg,
            "filename_prefix": f"local_video/p{project_id}_f{frame_id}_{label}",
        }
        graph = apply_params(workflow_id, params, uploaded_images=uploads)
        prompt_id = await comfy.queue_prompt(graph)
        history = await comfy.wait_for_prompt(prompt_id)
        outputs = comfy.collect_outputs(history)
        if not outputs:
            raise RuntimeError("ComfyUI produced no outputs")
        media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
        media.mkdir(parents=True, exist_ok=True)
        out = outputs[0]
        dest = media / out["filename"]
        await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
        return dest

    # Default FLF when both endpoints exist; otherwise I2V from start.
    use_flf = end_image is not None and (
        not workflow_id
        or workflow_id in ("wan22_flf2v", "ltx_flf2v", wfs["flf2v"])
    )
    if use_flf:
        return await backend.render_flf2v(
            project_id=project_id,
            frame_id=frame_id,
            start_image=start_image,
            end_image=end_image,
            prompt=prompt,
            label=label,
            num_frames=num_frames,
            seed=seed,
        )

    return await backend.render_i2v(
        project_id=project_id,
        frame_id=frame_id,
        start_image=start_image,
        prompt=prompt,
        label=label,
        num_frames=num_frames,
        seed=seed,
    )


async def _run_flf2v_two_pass(
    *,
    project_id: int,
    frame_id: int,
    start_image: Path,
    end_image: Path,
    prompt: str,
    label: str,
    num_frames: int,
    seed: int,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    dest_dir: Path | None = None,
    filename_prefix: str | None = None,
    negative_prompt: str | None = None,
) -> Path:
    """FLF2V as high-noise then low-noise prompts (avoids dual-UNET crash)."""
    settings = get_settings()
    validate_frame_count(num_frames)
    comfy = ComfyUIClient()
    neg = negative_prompt or (
        "blurry, watermark, text, static, jump cut, morphing face, flickering, "
        "collage, comic, storyboard, panels, grid, split screen, montage"
    )
    uploads = {
        "start_image": await comfy.upload_image(start_image),
        "end_image": await comfy.upload_image(end_image),
    }
    shared = {
        "positive_prompt": prompt,
        "negative_prompt": neg,
        "num_frames": num_frames,
        "width": width if width is not None else settings.default_width,
        "height": height if height is not None else settings.default_height,
    }
    prefix = filename_prefix or f"local_video/p{project_id}_f{frame_id}_{label}"

    # Pass 1: high-noise only → SaveLatent
    # Avoid POST /free on this ROCm host — it can kill the ComfyUI process.
    # Separate prompts still let Comfy unload the high UNET before loading low.
    high_params = {
        **shared,
        "seed": seed,
        "latent_prefix": f"latents/{prefix}_high",
    }
    high_graph = apply_params("wan22_flf2v_high", high_params, uploaded_images=uploads)
    high_id = await comfy.queue_prompt(high_graph)
    high_hist = await comfy.wait_for_prompt(high_id)
    latents = [
        o for o in comfy.collect_outputs(high_hist) if o.get("kind") == "latents"
    ]
    if not latents:
        # Some Comfy builds nest SaveLatent under outputs without a kind we expect.
        for _nid, node_out in (high_hist.get("outputs") or {}).items():
            for item in node_out.get("latents") or []:
                latents.append(
                    {
                        "kind": "latents",
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder") or "",
                        "type": item.get("type") or "output",
                    }
                )
    if not latents or not latents[0].get("filename"):
        raise RuntimeError("FLF2V high pass produced no latent output")
    latent_ref = comfy.latent_annotated_path(latents[0])

    # Brief pause so the high-noise model can leave GPU before low-noise loads.
    await asyncio.sleep(2)

    # Pass 2: low-noise + tiled decode → video
    low_params = {
        **shared,
        "fps": fps if fps is not None else settings.default_fps,
        "filename_prefix": prefix,
        "latent_file": latent_ref,
    }
    low_graph = apply_params("wan22_flf2v_low", low_params, uploaded_images=uploads)
    low_id = await comfy.queue_prompt(low_graph)
    low_hist = await comfy.wait_for_prompt(low_id)
    outputs = [
        o
        for o in comfy.collect_outputs(low_hist)
        if o.get("kind") in ("videos", "gifs", "images")
    ]
    if not outputs:
        raise RuntimeError("FLF2V low pass produced no video output")

    media = dest_dir or (
        settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    )
    media.mkdir(parents=True, exist_ok=True)
    out = outputs[0]
    dest = media / out["filename"]
    await comfy.download_view(out["filename"], dest, out["subfolder"], out["type"])
    return dest


async def _i2v_clip_from_image(
    *,
    project_id: int,
    frame_id: int,
    start_image: Path,
    prompt: str,
    label: str,
    num_frames: int,
    seed: int,
    workflow_id: str = "wan22_i2v",
) -> Path:
    """Legacy single-image I2V helper (start frame only)."""
    return await _bridge_clip_between_images(
        project_id=project_id,
        frame_id=frame_id,
        start_image=start_image,
        end_image=None,
        prompt=prompt,
        label=label,
        num_frames=num_frames,
        seed=seed,
        workflow_id=workflow_id,
    )


async def generate_step_clips(
    project_id: int,
    frame_id: int,
    *,
    num_frames: int = 33,
    workflow_id: str | None = None,
    video_backend: str | None = None,
) -> dict[str, Any]:
    """FLF2V between consecutive keyframes in the series; concat into preview_path."""
    with SessionLocal() as db:
        f = db.get(StoryboardFrame, frame_id)
        if not f or f.project_id != project_id:
            raise KeyError(f"frame {frame_id} not found")
        p = db.get(Project, project_id)
        assert p is not None
        keyframes = _keyframes_list(f)
        beat = f.visual_prompt or f.description or ""
        premise = p.premise or ""
        if len(keyframes) < 2 or not _keyframes_ready(keyframes):
            raise ValueError(
                "frame needs a complete keyframe series (at least first and last)"
            )

    settings = get_settings()
    media = settings.media_dir / "projects" / str(project_id) / "frames" / str(frame_id)
    media.mkdir(parents=True, exist_ok=True)

    clip_paths: list[Path] = []
    frame_dirs: list[Path] = []
    for i in range(len(keyframes) - 1):
        a = keyframes[i]
        b = keyframes[i + 1]
        start_p = _resolve_media_file(a["path"])
        end_p = _resolve_media_file(b["path"])
        clip = await _bridge_clip_between_images(
            project_id=project_id,
            frame_id=frame_id,
            start_image=start_p,
            end_image=end_p,
            prompt=build_transition_prompt(
                premise=premise,
                start_prompt=a.get("image_prompt") or f"t={a.get('t_sec')}s: {beat}",
                end_prompt=b.get("image_prompt") or f"t={b.get('t_sec')}s: {beat}",
            ),
            label=f"clip_{i:02d}",
            num_frames=num_frames,
            seed=frame_id * 17 + 11 + i,
            workflow_id=workflow_id,
            video_backend=video_backend,
        )
        clip_paths.append(clip)
        raw = media / f"_clip_{i:02d}_frames"
        extract_frames_from_video(clip, raw)
        frame_dirs.append(raw)

    preview = media / f"step_preview_f{frame_id}.mp4"
    if frame_dirs and all(any(d.glob("*.png")) for d in frame_dirs):
        from app.services.frames import discard_overlap, write_kept_frames

        # Drop the shared boundary frame between consecutive FLF clips.
        kept_dirs: list[Path] = []
        for i, raw in enumerate(frame_dirs):
            frames_list = sorted(raw.glob("*.png"))
            if i > 0:
                frames_list = discard_overlap(frames_list, 1)
            kept = media / f"_clip_{i:02d}_kept"
            write_kept_frames(frames_list, kept)
            kept_dirs.append(kept)
        seq = media / "_step_seq"
        concat_frame_dirs(kept_dirs, seq)
        encode_frames_to_mp4(seq, preview, fps=settings.default_fps)
    else:
        concat_videos(clip_paths, preview)

    with SessionLocal() as db:
        fr = db.get(StoryboardFrame, frame_id)
        assert fr
        fr.preview_path = str(preview)
        db.commit()

    return {
        "frame_id": frame_id,
        "kind": "step_clips",
        "preview_path": str(preview),
        "clips": [str(c) for c in clip_paths],
        "keyframes": keyframes,
        "keyframe_last_path": keyframes[-1].get("path"),
        "video_backend": video_backend,
    }


async def generate_all_step_clips(
    project_id: int,
    *,
    skip_existing: bool = True,
    num_frames: int = 33,
    video_backend: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if not p:
            raise KeyError(f"project {project_id} not found")
        frames = [
            {
                "id": f.id,
                "preview_path": f.preview_path,
                "ready": _keyframes_ready(_keyframes_list(f)),
            }
            for f in sorted(p.frames, key=lambda x: x.position)
        ]
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped = 0
    for fr in frames:
        if not fr["ready"]:
            errors.append({"frame_id": fr["id"], "error": "missing keyframes"})
            continue
        if skip_existing and fr.get("preview_path"):
            results.append({"frame_id": fr["id"], "skipped": True})
            skipped += 1
            continue
        try:
            results.append(
                await generate_step_clips(
                    project_id,
                    fr["id"],
                    num_frames=num_frames,
                    video_backend=video_backend,
                    workflow_id=workflow_id,
                )
            )
        except Exception as e:
            errors.append({"frame_id": fr["id"], "error": str(e)})
    return {
        "project_id": project_id,
        "generated": len([r for r in results if not r.get("skipped")]),
        "skipped": skipped,
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }
