from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings

# Rough chars/token for English+JSON prompts when estimating budget.
_CHARS_PER_TOKEN = 4
# Keep headroom for model formatting / safety.
_CTX_SAFETY = 256


def _client() -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(base_url=settings.llama_base_url, api_key=settings.llama_api_key)


def _effective_n_ctx() -> int:
    settings = get_settings()
    if settings.llama_n_ctx and settings.llama_n_ctx > 0:
        return int(settings.llama_n_ctx)
    # Sensible default when model meta wasn't saved yet.
    return 8192


def _budget_tokens() -> tuple[int, int]:
    """Return (n_ctx, max_tokens) for the next completion."""
    settings = get_settings()
    n_ctx = _effective_n_ctx()
    # Leave room for the prompt; never ask for more completion than ~1/4 of ctx.
    configured = int(settings.llama_max_tokens or 2048)
    max_tokens = min(configured, max(256, n_ctx // 4))
    max_tokens = min(max_tokens, max(256, n_ctx - _CTX_SAFETY - 64))
    return n_ctx, max_tokens


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    max_chars = max_tokens * _CHARS_PER_TOKEN
    text = text or ""
    if len(text) <= max_chars:
        return text
    keep_head = int(max_chars * 0.65)
    keep_tail = max_chars - keep_head - 40
    if keep_tail < 80:
        return text[: max_chars - 20] + "\n…[truncated]…"
    return text[:keep_head] + "\n…[truncated for context window]…\n" + text[-keep_tail:]


async def chat(system: str, user: str, temperature: float = 0.4) -> str:
    settings = get_settings()
    n_ctx, max_tokens = _budget_tokens()
    system = system or ""
    user = user or ""
    sys_tokens = max(1, len(system) // _CHARS_PER_TOKEN)
    # Prompt budget = ctx - completion - safety
    prompt_budget = max(256, n_ctx - max_tokens - _CTX_SAFETY)
    user_budget = max(128, prompt_budget - sys_tokens)
    user = _truncate_to_tokens(user, user_budget)

    client = _client()
    resp = await client.chat.completions.create(
        model=settings.llama_model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def _repair_json_text(text: str) -> str:
    """Best-effort fixes for small-model JSON (trailing commas, fences)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    # Trailing commas before } or ]
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return t.strip()


def _extract_json(text: str) -> Any:
    """Parse JSON from a model reply; prefer the first array/object; reject junk tails."""
    cleaned = _repair_json_text(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Prefer first JSON array (storyboards), then first object.
    for pattern in (r"\[[\s\S]*\]", r"\{[\s\S]*\}"):
        match = re.search(pattern, cleaned)
        if not match:
            continue
        chunk = _repair_json_text(match.group(0))
        decoder = json.JSONDecoder()
        try:
            value, _end = decoder.raw_decode(chunk)
            return value
        except json.JSONDecodeError:
            # Truncate to last closing bracket and retry.
            if chunk.startswith("["):
                end = chunk.rfind("]")
            else:
                end = chunk.rfind("}")
            if end > 0:
                try:
                    return json.loads(_repair_json_text(chunk[: end + 1]))
                except json.JSONDecodeError:
                    continue
    raise ValueError(f"could not parse JSON from model reply: {cleaned[:400]!r}")


def _story_excerpt_for_structure(story: str, *, max_chars: int = 5500) -> str:
    """Send a bounded excerpt for propose/cast — full novellas break small models."""
    s = (story or "").strip()
    if len(s) <= max_chars:
        return s
    head = int(max_chars * 0.65)
    tail = max_chars - head - 60
    return (
        s[:head].rstrip()
        + "\n\n[...middle of story omitted for length...]\n\n"
        + s[-tail:].lstrip()
    )


async def generate_story(title: str, genre: str, premise: str) -> str:
    system = (
        "You are a screenwriter for short cinematic videos. "
        "Write a concise story with clear scenes and visual beats. Plain text only. "
        "Give recurring characters stable proper names and consistent visual identity."
    )
    user = f"Title: {title}\nGenre: {genre}\nPremise: {premise}\n\nWrite the story."
    return await chat(system, user)


async def extend_story(story: str, instruction: str) -> str:
    system = "You extend film stories while keeping continuity of characters, tone, and setting."
    user = f"Current story:\n{story}\n\nInstruction:\n{instruction}\n\nReturn the full updated story."
    return await chat(system, user)


_CLOTHING_LINE_RE = re.compile(
    r"(?i)\b("
    r"space\s*suit|spacesuit|astronaut|eva\s*suit|helmet|wardrobe|outfit|"
    r"dress|shirt|shorts|pants|jacket|coat|gloves?|mittens?|gauntlets?|"
    r"boots|sneakers|shoes|"
    r"clothing|clothes|garment|suit\b"
    r")\b"
)
_FACE_LINE_RE = re.compile(
    r"(?i)\b(face|hair|eye|eyes|smile|skin|age|years?\s*old|blonde|brunette|"
    r"rectangular|skinnier|thinner|teeth|expression|nose|cheek)\b"
)
_GLOVE_HAND_RE = re.compile(
    r"(?i)\b(glove|mitten|gauntlet|fingers?\s+on\s+each\s+hand|space\s*glove)\b"
)
_ANIMATED_GENRE_RE = re.compile(
    r"(?i)(animated|animation|puppet|cgi|3\s*d|pixar|cartoon|stylized|"
    r"illustration|render|stop[\s-]?motion)"
)


def _scrub_glove_hand_clauses(line: str) -> str:
    """Drop glove/finger-count crumbs that bleed from spacesuit appearance edits."""
    text = re.sub(r"(?i)\([^)]*\b(glove|mitten|gauntlet)[^)]*\)", "", line)
    text = re.sub(
        r"(?i)\b(?:and\s+)?\d+\s*fingers?\s+on\s+each\s+hand\b",
        "",
        text,
    )
    text = _GLOVE_HAND_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r",\s*,+", ",", text)
    return text.strip(" ,.;-")


def face_body_lock(appearance: str, *, has_wardrobe: bool) -> str:
    """Keep face/body identity; drop clothing notes when an outfit is selected.

    Character appearance often accumulates outfit edit history (e.g. spacesuit)
    that must not override a beat-specific wardrobe. Face/hair Applied edits stay.
    """
    text = (appearance or "").strip()
    if not text:
        return ""
    if not has_wardrobe:
        return text

    before = text
    edit_lines: list[str] = []
    if "Applied edits:" in text:
        before, after = text.split("Applied edits:", 1)
        before = before.strip()
        for raw in after.splitlines():
            line = raw.strip(" -•\t")
            if not line:
                continue
            has_clothes = bool(_CLOTHING_LINE_RE.search(line))
            has_face = bool(_FACE_LINE_RE.search(line))
            if has_clothes and not has_face:
                continue
            if has_clothes and has_face:
                line = _scrub_glove_hand_clauses(line)
                line = _CLOTHING_LINE_RE.sub("", line)
                line = re.sub(r"\s{2,}", " ", line).strip(" ,.;-")
                line = re.sub(
                    r"(?i)\b(also,?\s*)?(it should be a|make (him|her|them)|put cool stuff on the)\b.*$",
                    "",
                    line,
                ).strip(" ,.;-")
                # Drop empty leftover parentheses after scrubbing.
                line = re.sub(r"\(\s*\)", "", line)
                line = re.sub(r"\s{2,}", " ", line).strip(" ,.;-")
            elif _GLOVE_HAND_RE.search(line) and not has_face:
                continue
            elif _GLOVE_HAND_RE.search(line):
                line = _scrub_glove_hand_clauses(line)
                line = re.sub(r"\(\s*\)", "", line).strip(" ,.;-")
            if line:
                edit_lines.append(line)
        text = before

    kept: list[str] = []
    for raw in text.replace(";", "\n").splitlines():
        line = raw.strip(" -•\t")
        if not line:
            continue
        if _CLOTHING_LINE_RE.search(line) and not _FACE_LINE_RE.search(line):
            continue
        if _CLOTHING_LINE_RE.search(line):
            line = _CLOTHING_LINE_RE.sub("", line)
            line = _scrub_glove_hand_clauses(line)
            line = re.sub(r"\s{2,}", " ", line).strip(" ,.;-")
            if not line:
                continue
        kept.append(line)
    kept.extend(edit_lines)
    return " ".join(kept).strip()


async def describe_outfit_from_character_look(
    *,
    name: str,
    appearance: str,
    extra_instruction: str | None = None,
) -> dict[str, str]:
    """Infer a wardrobe outfit card from a character portrait look description.

    Returns ``{"name": short outfit label, "prompt": clothing-only prompt}``.
    """
    look = (appearance or "").strip()
    who = (name or "Character").strip() or "Character"
    extra = (extra_instruction or "").strip()
    system = (
        "You write wardrobe cards for film continuity. Return ONLY valid JSON: "
        '{"name": str, "prompt": str}. '
        'name = short outfit label (2–4 words, e.g. "Yellow summer dress"). '
        "prompt = clothing/footwear/accessories ONLY — colors, materials, garments. "
        "No face, hair, age, body type, pose, or background. Concise (under 40 words)."
    )
    user = f"Character: {who}\nFull look notes:\n{look or '(none)'}\n"
    if extra:
        user += f"Latest edit direction (may change clothes):\n{extra}\n"
    user += "Describe the outfit visible / implied for this character portrait as JSON."
    try:
        raw = await chat(system, user, temperature=0.2)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            raise ValueError("outfit describe response was not an object")
        oname = str(data.get("name") or "").strip() or "From portrait"
        prompt = str(data.get("prompt") or data.get("wardrobe") or "").strip()
        if not prompt:
            raise ValueError("empty outfit prompt")
        return {"name": oname[:48], "prompt": prompt}
    except Exception:
        # Text-only fallback when the LLM is down or returns junk.
        fallback = look or "clothing as shown in the character portrait"
        return {
            "name": "From portrait",
            "prompt": f"Wardrobe from character portrait: {fallback[:280]}",
        }


def wardrobe_conflict_negatives(appearance: str, wardrobe: str) -> str:
    """Negative tokens for clothing mentioned in appearance but not in wardrobe."""
    app = (appearance or "").lower()
    ward = (wardrobe or "").lower()
    if not ward:
        return ""
    bits: list[str] = []
    if re.search(
        r"(?i)space\s*suit|spacesuit|astronaut|eva\s*suit", app
    ) and not re.search(r"(?i)space\s*suit|spacesuit|astronaut|eva\s*suit", ward):
        bits.append(
            "astronaut suit, spacesuit, space suit, EVA suit, helmet, NASA suit, "
            "pressure suit, white space suit"
        )
    if re.search(r"(?i)glove|mitten|gauntlet", app) and not re.search(
        r"(?i)glove|mitten|gauntlet", ward
    ):
        bits.append("gloves, mittens, gauntlets, space gloves, cartoon gloves")
    return ", ".join(bits)


def wardrobe_gap_negatives(wardrobe: str) -> str:
    """Negate accessories Flux often invents when the outfit text omits them.

    Only uses the outfit prompt itself — no scene-specific hardcodes.
    """
    ward = (wardrobe or "").lower()
    if not ward:
        return ""
    bits: list[str] = []
    if not re.search(r"glove|mitten", ward):
        bits.append(
            "gloves, mittens, gauntlets, space gloves, cartoon gloves, "
            "white gloves, black gloves, oversized gloves"
        )
    if not re.search(r"helmet", ward):
        bits.append("helmet")
    return ", ".join(bits)


def strip_positive_anti_prompts(prompt: str) -> str:
    """Remove 'NO GLOVES'-style bans from positives (they often summon the item)."""
    text = (prompt or "").strip()
    if not text:
        return ""
    # Explicit all-caps bans and "no gloves" / "without gloves" phrasing.
    text = re.sub(r"(?i)\bno\s+gloves\.?", "bare hands", text)
    text = re.sub(r"(?i)\bwithout\s+gloves\.?", "bare hands", text)
    text = re.sub(
        r"\bNO\s+(?:GLOVES|MITTENS|GAUNTLETS|HELMETS?)(?:\s+(?:OR|AND)\s+"
        r"(?:GLOVES|MITTENS|GAUNTLETS|HELMETS?))*\.?",
        "",
        text,
    )
    return re.sub(r"\s{2,}", " ", text).strip(" ,.;")


def is_stylized_genre(genre: str = "") -> bool:
    return bool(_ANIMATED_GENRE_RE.search(genre or ""))


def style_lock_phrase(genre: str = "") -> str:
    """Opening style cue for stills — follow project genre, not always photoreal."""
    g = (genre or "").strip()
    if is_stylized_genre(g):
        return (
            f"{g} style: match the cast reference art style exactly "
            "(stylized look from the cast refs), one continuous camera shot, one moment only"
        )
    if g:
        return (
            f"{g} genre cinematic still, one continuous camera shot, one moment only, "
            "full frame"
        )
    return "Cinematic still, one continuous camera shot, one moment only, full frame"


def style_negatives(genre: str = "") -> str:
    if is_stylized_genre(genre):
        return (
            "photorealistic human, live action, real photograph, documentary photo, "
            "realistic skin pores, real person, stock photo"
        )
    return ""


def format_cast_sheet(characters: list[dict[str, Any]]) -> str:
    """Compact ground-truth cast block for image/storyboard prompts."""
    lines: list[str] = []
    for c in characters:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        look_raw = (c.get("appearance_prompt") or c.get("description") or "").strip()
        wardrobe = (c.get("wardrobe_prompt") or "").strip()
        outfit_name = (c.get("outfit_name") or "").strip()
        look = face_body_lock(look_raw, has_wardrobe=bool(wardrobe))
        bits: list[str] = []
        if look:
            bits.append(f"Face/body: {look}")
        if wardrobe:
            label = f"Wardrobe ({outfit_name})" if outfit_name else "Wardrobe"
            hand_note = (
                "; bare hands" if not re.search(r"(?i)glove|mitten", wardrobe) else ""
            )
            bits.append(
                f"{label}: {wardrobe}{hand_note} "
                "(MUST wear exactly this clothing; ignore any other outfit notes)"
            )
        elif look_raw and not look:
            bits.append(look_raw)
        if bits:
            lines.append(f"- {name}: " + ". ".join(bits))
        else:
            lines.append(f"- {name}")
    if not lines:
        return ""
    return "Cast lock (use these exact names, looks, and wardrobes):\n" + "\n".join(
        lines
    )


async def extract_cast(story: str) -> list[dict[str, Any]]:
    """Pull distinct characters from a story for the cast sheet."""
    system = (
        "You extract the cast for a short film. Return ONLY valid JSON. "
        "Prefer a raw array. Also accept "
        '{"characters": [...]} or {"cast": [...]}. '
        "Each item: "
        '{"name": str, "aliases": [str], "description": str, "appearance_prompt": str}. '
        "Include only named or clearly recurring characters (not crowds). "
        "appearance_prompt must be a concrete visual look: age range, face, hair, wardrobe, "
        "distinctive details — suitable for image generation. Keep each field concise."
    )
    excerpt = _story_excerpt_for_structure(story)
    user = f"Story (may be excerpted):\n{excerpt}\n\nExtract the cast as JSON."
    raw = await chat(system, user, temperature=0.2)
    try:
        data = _extract_json(raw)
    except Exception as e:
        snippet = (raw or "").strip().replace("\n", " ")[:240]
        raise ValueError(
            f"cast JSON parse failed: {e}. Model replied: {snippet or '(empty)'}"
        ) from e

    if isinstance(data, dict):
        for key in ("characters", "cast", "people", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            # Single character object
            if data.get("name"):
                data = [data]
            else:
                raise ValueError(
                    "cast response was an object without a characters/cast array"
                )
    if not isinstance(data, list):
        raise ValueError("cast response was not a list")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = []
        out.append(
            {
                "name": name,
                "aliases": [str(a).strip() for a in aliases if str(a).strip()],
                "description": str(item.get("description") or "").strip(),
                "appearance_prompt": str(
                    item.get("appearance_prompt") or item.get("look") or ""
                ).strip(),
            }
        )
    if not out:
        snippet = (raw or "").strip().replace("\n", " ")[:240]
        raise ValueError(
            f"no characters extracted from story. Model replied: {snippet or '(empty)'}"
        )
    return out


def _normalize_proposed_frames(
    data: list[Any],
    *,
    max_frames: int,
    default_duration: float = 4.0,
    start_position: int = 0,
) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for i, item in enumerate(data[:max_frames]):
        if not isinstance(item, dict):
            continue
        frames.append(
            {
                "position": start_position + i,
                "description": str(item.get("description") or ""),
                "visual_prompt": str(
                    item.get("visual_prompt") or item.get("description") or ""
                ),
                "duration_hint_sec": float(
                    item.get("duration_hint_sec") or default_duration
                ),
                "is_new_shot": bool(item.get("is_new_shot", True)),
            }
        )
    return frames


async def propose_storyboard(
    story: str,
    max_frames: int = 8,
    cast_sheet: str = "",
    *,
    avg_beat_sec: float | None = None,
    target_duration_sec: float | None = None,
) -> list[dict[str, Any]]:
    default_dur = float(avg_beat_sec) if avg_beat_sec and avg_beat_sec > 0 else 4.0
    excerpt = _story_excerpt_for_structure(story)
    system = (
        "You break stories into storyboard frames for video generation. "
        "Return ONLY a valid JSON array (no prose). Each item: "
        '{"description": str, "visual_prompt": str, "duration_hint_sec": number, "is_new_shot": bool}. '
        "is_new_shot true when camera/scene changes; false for continuous action. "
        "Keep characters, wardrobe, setting, era, and visual style consistent across all frames. "
        "Each visual_prompt should name recurring subjects with their canonical cast names. "
        "When is_new_shot is false, the beat continues the previous shot's motion and camera. "
        f"Aim for EXACTLY {max_frames} frames (not fewer)."
    )
    user = f"Story (may be excerpted):\n{excerpt}\n\n"
    if cast_sheet:
        user += f"{cast_sheet}\n\n"
    if target_duration_sec and target_duration_sec > 0:
        user += (
            f"Target total runtime ≈ {float(target_duration_sec):.0f}s; "
            f"prefer ~{default_dur:.0f}s per beat.\n"
        )
    user += f"Return a JSON array with exactly {max_frames} frames covering the full arc."
    try:
        raw = await chat(system, user, temperature=0.3)
        data = _extract_json(raw)
    except Exception as e:
        raise ValueError(
            f"storyboard propose failed to parse JSON: {e}. "
            f"Raw reply starts: {(locals().get('raw') or '')[:500]!r}"
        ) from e
    if not isinstance(data, list):
        raise ValueError(
            f"storyboard response was not a list (got {type(data).__name__}). "
            f"Raw reply starts: {raw[:500]!r}"
        )
    frames = _normalize_proposed_frames(
        data, max_frames=max_frames, default_duration=default_dur
    )
    if not frames:
        raise ValueError(
            "storyboard propose returned an empty list — board left unchanged. "
            f"Raw reply starts: {raw[:500]!r}"
        )

    # Continue-generation if the small model under-delivered.
    retries = 0
    while len(frames) < max_frames and retries < 2:
        need = max_frames - len(frames)
        more = await continue_storyboard(
            story,
            frames,
            need=need,
            cast_sheet=cast_sheet,
            avg_beat_sec=default_dur,
        )
        if not more:
            break
        frames.extend(more)
        retries += 1

    # Pad placeholders so agents get the requested count (editable later).
    while len(frames) < max_frames:
        i = len(frames)
        frames.append(
            {
                "position": i,
                "description": f"[Placeholder beat {i + 1} — expand from story]",
                "visual_prompt": (
                    f"Continue the story visually for beat {i + 1}; "
                    "same cast, wardrobe, and setting."
                ),
                "duration_hint_sec": default_dur,
                "is_new_shot": True,
            }
        )
    # Re-index after continues/pads.
    for i, fr in enumerate(frames[:max_frames]):
        fr["position"] = i
    return frames[:max_frames]


async def continue_storyboard(
    story: str,
    existing: list[dict[str, Any]],
    *,
    need: int,
    cast_sheet: str = "",
    avg_beat_sec: float = 4.0,
) -> list[dict[str, Any]]:
    """Ask the model for more beats after an under-count propose."""
    if need <= 0:
        return []
    excerpt = _story_excerpt_for_structure(story)
    prior = [
        {
            "description": f.get("description"),
            "visual_prompt": f.get("visual_prompt"),
            "duration_hint_sec": f.get("duration_hint_sec"),
            "is_new_shot": f.get("is_new_shot"),
        }
        for f in existing[-8:]
    ]
    system = (
        "You continue a storyboard. Return ONLY a valid JSON array of NEW frames "
        '(same schema: description, visual_prompt, duration_hint_sec, is_new_shot). '
        "Do not repeat prior beats; advance the story."
    )
    user = (
        f"Story (may be excerpted):\n{excerpt}\n\n"
        f"Existing beats (tail):\n{json.dumps(prior, ensure_ascii=False)}\n\n"
    )
    if cast_sheet:
        user += f"{cast_sheet}\n\n"
    user += f"Add exactly {need} more frames (~{avg_beat_sec:.0f}s each)."
    try:
        raw = await chat(system, user, temperature=0.3)
        data = _extract_json(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return _normalize_proposed_frames(
        data,
        max_frames=need,
        default_duration=avg_beat_sec,
        start_position=len(existing),
    )


async def prompt_delta_for_continue(
    prompt_base: str, previous_delta: str, continuity_notes: str
) -> str:
    system = (
        "You write short motion/camera continuation deltas for Wan video chunks. "
        "Do NOT restate identity, wardrobe, or location (those are locked in prompt_base). "
        "Return one short sentence only."
    )
    user = (
        f"prompt_base: {prompt_base}\n"
        f"previous_delta: {previous_delta}\n"
        f"continuity_notes: {continuity_notes}\n"
        "Write the next prompt_delta."
    )
    return await chat(system, user, temperature=0.3)


def keyframe_plan_times(duration_sec: float) -> list[float]:
    """Keyframe times so consecutive spacing is at most ~2s; always include 0 and D."""
    import math

    d = max(0.5, float(duration_sec or 4.0))
    n_middle = max(0, math.ceil(d / 2.0) - 1)
    total = n_middle + 2
    if total == 2:
        return [0.0, round(d, 3)]
    step = d / (total - 1)
    return [round(i * step, 3) for i in range(total)]


async def plan_keyframe_image_prompt(
    *,
    description: str,
    visual: str,
    t_sec: float,
    role: str,
    is_new_shot: bool,
    prev_prompt: str | None = None,
    first_prompt: str | None = None,
    last_goal: str | None = None,
    cast_sheet: str = "",
) -> str:
    """One self-contained image prompt for a keyframe slot (Comfy sees only this)."""
    system = (
        "You write ONE cinematic still image prompt for a moment in a continuous shot. "
        'Return ONLY valid JSON: {"image_prompt": "..."}. '
        "Rules: one moment; concrete action/pose/camera/light/setting; no collage or panels; "
        "no whole-film dump; never invent subjects not in the beat; do not say the word keyframe. "
        "CRITICAL — character looks are NOT part of this prompt: do not describe face, hair, age, "
        "skin, body type, clothing, outfits, wardrobe, gloves, helmets, shoes, or accessories. "
        "Those come only from a separate cast lock applied at render time. "
        "If a cast lock is provided, name those characters exactly when they appear, then describe "
        "only what they are doing and how the camera sees the moment. "
        "Never invent people, props-on-characters, or outfit changes. "
        "If CONTINUATION: do not say new shot; keep the same named people and setting. "
        "If NEW SHOT and role is first: establish a fresh camera/composition. "
        "Never imply an outfit, shoe, or accessory change between frames — wardrobe is fixed. "
        "If previous prompts include look/wardrobe text, IGNORE that look text — do not copy it."
    )
    shot = (
        "NEW SHOT — independent keyframe series; fresh camera for the first frame."
        if is_new_shot
        else "CONTINUATION of the same shot — do NOT call this a new shot."
    )
    user = (
        f"Shot type: {shot}\n"
        f"Beat description: {description}\n"
        f"Beat visual note: {visual}\n"
        f"This frame role={role} at t={t_sec}s.\n"
        "Write image_prompt for action, pose, camera, light, and environment only.\n"
    )
    if cast_sheet:
        user += (
            f"{cast_sheet}\n"
            "Use only the cast names above. Do NOT paste Face/body or Wardrobe lines "
            "into image_prompt — looks are applied separately.\n"
        )
    if prev_prompt:
        user += (
            "Previous frame prompt (continue action/camera only; ignore any look text):\n"
            f"{prev_prompt}\n"
        )
    if first_prompt and role != "first":
        user += (
            "Shot first prompt (action/camera continuity only; ignore look text):\n"
            f"{first_prompt}\n"
        )
    if last_goal and role != "last":
        user += (
            "Shot should end toward (action/camera only; ignore look text):\n"
            f"{last_goal}\n"
        )
    if role == "last":
        user += "Write the ENDING still of this shot."
    elif role == "first":
        user += "Write the STARTING still of this shot."
    else:
        user += "Write a MIDDLE still progressing from previous toward the ending goal."
    raw = await chat(system, user, temperature=0.25)
    data = _extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("keyframe prompt response was not an object")
    prompt = str(data.get("image_prompt") or data.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("empty keyframe image_prompt")
    return strip_positive_anti_prompts(prompt)


async def plan_keyframe_series(
    *,
    description: str,
    visual: str,
    duration_sec: float,
    is_new_shot: bool,
    prev_last_prompt: str | None = None,
    cast_sheet: str = "",
    share_first_from_prev: bool = False,
) -> dict[str, Any]:
    """Plan first + optional ≤2s middles + last via per-slot LLM calls.

    When share_first_from_prev is True, the first slot reuses prev_last_prompt
    exactly so the opening keyframe matches the prior ending.
    """
    times = keyframe_plan_times(duration_sec)
    roles = []
    for i, _t in enumerate(times):
        if i == 0:
            roles.append("first")
        elif i == len(times) - 1:
            roles.append("last")
        else:
            roles.append("middle")

    shared = bool(share_first_from_prev and (prev_last_prompt or "").strip())
    if shared:
        first_prompt = (prev_last_prompt or "").strip()
    else:
        # Plan last first for arc, then first (original order).
        last_goal = await plan_keyframe_image_prompt(
            description=description,
            visual=visual,
            t_sec=times[-1],
            role="last",
            is_new_shot=is_new_shot,
            prev_prompt=prev_last_prompt if not is_new_shot else None,
            cast_sheet=cast_sheet,
        )
        first_prompt = await plan_keyframe_image_prompt(
            description=description,
            visual=visual,
            t_sec=times[0],
            role="first",
            is_new_shot=is_new_shot,
            prev_prompt=prev_last_prompt if not is_new_shot else None,
            last_goal=last_goal,
            cast_sheet=cast_sheet,
        )

    if shared:
        last_goal = await plan_keyframe_image_prompt(
            description=description,
            visual=visual,
            t_sec=times[-1],
            role="last",
            is_new_shot=is_new_shot,
            prev_prompt=first_prompt,
            first_prompt=first_prompt,
            cast_sheet=cast_sheet,
        )

    keyframes: list[dict[str, Any]] = [
        {
            "index": 0,
            "t_sec": times[0],
            "role": "first",
            "image_prompt": first_prompt,
            "path": None,
        }
    ]
    prev = first_prompt
    for i, (t, role) in enumerate(zip(times[1:-1], roles[1:-1]), start=1):
        prompt = await plan_keyframe_image_prompt(
            description=description,
            visual=visual,
            t_sec=t,
            role=role,
            is_new_shot=is_new_shot,
            prev_prompt=prev,
            first_prompt=first_prompt,
            last_goal=last_goal,
            cast_sheet=cast_sheet,
        )
        keyframes.append(
            {
                "index": i,
                "t_sec": t,
                "role": role,
                "image_prompt": prompt,
                "path": None,
            }
        )
        prev = prompt
    keyframes.append(
        {
            "index": len(times) - 1,
            "t_sec": times[-1],
            "role": "last",
            "image_prompt": last_goal,
            "path": None,
        }
    )
    return {
        "duration_sec": times[-1],
        "is_new_shot": is_new_shot,
        "keyframes": keyframes,
        "shared_first": shared,
    }


async def plan_beat_audio_prompt(
    *,
    story: str,
    description: str,
    visual: str,
    duration_sec: float = 4.0,
    cast_names: list[str] | None = None,
    premise: str = "",
    existing_dialog: str = "",
    enrich_only: bool = False,
) -> dict[str, str]:
    """LLM: spoken dialog and/or SFX notes for one beat.

    Returns ``{"dialog": ..., "audio_notes": ...}``. When ``enrich_only`` and
    existing dialog is set, preserve speech and only add SFX/music notes.
    """
    dur = float(duration_sec or 4.0)
    # Rough speech budget: ~2.5 words/sec for kids pacing, leave room for pauses.
    speech_sec = max(2.0, min(dur * 0.55, dur - 1.0))
    names = [n for n in (cast_names or []) if (n or "").strip()]
    story_ctx = _story_excerpt_for_structure(story, max_chars=4000)

    if enrich_only and (existing_dialog or "").strip():
        system = (
            "You enrich AUDIO for ONE film beat. Return ONLY valid JSON: "
            '{"audio_notes": "..."}. '
            "Keep existing spoken dialog unchanged (do not rewrite it). "
            "Add brief SFX / music / ambient notes that fit this moment. "
            "No visual/camera description."
        )
        user = (
            f"Beat duration ≈ {dur:.1f}s.\n"
            f"Beat description: {description}\n"
            f"Beat visual note: {visual}\n"
            f"Existing spoken dialog (KEEP AS-IS):\n{existing_dialog.strip()}\n"
        )
        if premise:
            user += f"Premise: {premise}\n"
        if names:
            user += "Cast names: " + ", ".join(names) + "\n"
        user += "Write audio_notes (SFX/music only) for this beat."
        raw = await chat(system, user, temperature=0.3)
        data = _extract_json(raw)
        notes = ""
        if isinstance(data, dict):
            notes = str(
                data.get("audio_notes") or data.get("audio_prompt") or ""
            ).strip()
        elif isinstance(data, str):
            notes = data.strip()
        if not notes:
            raise ValueError("empty audio_notes from model")
        return {"dialog": existing_dialog.strip(), "audio_notes": notes}

    system = (
        "You write AUDIO for ONE short film beat. Return ONLY valid JSON: "
        '{"dialog": "...", "audio_notes": "..."}. '
        'dialog = spoken lines (Character: "line") sized for the beat duration; '
        "audio_notes = brief ambient SFX / music only. "
        "Use exact cast names when provided. Do not invent major plot events. "
        "No visual/camera description. If silent, dialog may be empty and "
        "audio_notes lists ambient sound."
    )
    user = (
        f"Beat duration ≈ {dur:.1f}s — keep spoken dialog to roughly {speech_sec:.0f}s "
        f"of speech (~{int(speech_sec * 2.5)} words max).\n"
        f"Beat description: {description}\n"
        f"Beat visual note: {visual}\n"
    )
    if premise:
        user += f"Premise: {premise}\n"
    if names:
        user += "Cast names: " + ", ".join(names) + "\n"
    user += f"Story context:\n{story_ctx}\n\nWrite dialog and audio_notes for this beat only."
    raw = await chat(system, user, temperature=0.3)
    data = _extract_json(raw)
    dialog = ""
    notes = ""
    if isinstance(data, dict):
        dialog = str(
            data.get("dialog") or data.get("dialogue") or ""
        ).strip()
        notes = str(data.get("audio_notes") or "").strip()
        # Legacy single-field models.
        if not dialog and not notes:
            blob = str(
                data.get("audio_prompt") or data.get("prompt") or ""
            ).strip()
            dialog = blob
    elif isinstance(data, str):
        dialog = data.strip()
    if not dialog and not notes:
        raise ValueError("empty dialog/audio_notes from model")
    return {"dialog": dialog, "audio_notes": notes}
