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


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if match:
            return json.loads(match.group(1))
        raise


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
    r"dress|shirt|shorts|pants|jacket|coat|gloves|boots|sneakers|shoes|"
    r"clothing|clothes|garment|suit\b"
    r")\b"
)


def face_body_lock(appearance: str, *, has_wardrobe: bool) -> str:
    """Keep face/body identity; drop clothing notes when an outfit is selected.

    Character appearance often accumulates outfit edit history (e.g. spacesuit)
    that must not override a beat-specific wardrobe.
    """
    text = (appearance or "").strip()
    if not text:
        return ""
    if not has_wardrobe:
        return text
    if "Applied edits:" in text:
        text = text.split("Applied edits:", 1)[0].strip()
    kept: list[str] = []
    for raw in text.replace(";", "\n").splitlines():
        line = raw.strip(" -•\t")
        if not line:
            continue
        if _CLOTHING_LINE_RE.search(line):
            # Keep mixed lines only when they clearly describe face/hair/age.
            if not re.search(r"(?i)\b(face|hair|eye|smile|skin|age|years?\s*old)\b", line):
                continue
            line = _CLOTHING_LINE_RE.sub("", line)
            line = re.sub(r"\s{2,}", " ", line).strip(" ,.;-")
            if not line:
                continue
        kept.append(line)
    return " ".join(kept).strip()


def wardrobe_conflict_negatives(appearance: str, wardrobe: str) -> str:
    """Negative tokens for clothing mentioned in appearance but not in wardrobe."""
    app = (appearance or "").lower()
    ward = (wardrobe or "").lower()
    if not ward:
        return ""
    bits: list[str] = []
    if re.search(r"(?i)space\s*suit|spacesuit|astronaut|eva\s*suit", app) and not re.search(
        r"(?i)space\s*suit|spacesuit|astronaut|eva\s*suit", ward
    ):
        bits.append(
            "astronaut suit, spacesuit, space suit, EVA suit, helmet, NASA suit, "
            "pressure suit, white space suit"
        )
    return ", ".join(bits)


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
        if wardrobe:
            label = f"Wardrobe ({outfit_name})" if outfit_name else "Wardrobe"
            bits.append(
                f"{label}: {wardrobe} "
                "(MUST wear exactly this clothing; ignore any other outfit notes)"
            )
            if look:
                bits.append(f"Face/body only: {look}")
        elif look_raw:
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
        "You extract the cast for a short film. Return ONLY valid JSON array. "
        "Each item: "
        '{"name": str, "aliases": [str], "description": str, "appearance_prompt": str}. '
        "Include only named or clearly recurring characters (not crowds). "
        "appearance_prompt must be a concrete visual look: age range, face, hair, wardrobe, "
        "distinctive details — suitable for image generation. Keep each field concise."
    )
    user = f"Story:\n{story}\n\nExtract the cast."
    raw = await chat(system, user, temperature=0.2)
    data = _extract_json(raw)
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
    return out


async def propose_storyboard(
    story: str, max_frames: int = 8, cast_sheet: str = ""
) -> list[dict[str, Any]]:
    system = (
        "You break stories into storyboard frames for video generation. "
        "Return ONLY valid JSON array. Each item: "
        '{"description": str, "visual_prompt": str, "duration_hint_sec": number, "is_new_shot": bool}. '
        "is_new_shot true when camera/scene changes; false for continuous action. "
        "Keep characters, wardrobe, setting, era, and visual style consistent across all frames. "
        "Each visual_prompt should name recurring subjects with their canonical cast names. "
        "When is_new_shot is false, the beat continues the previous shot's motion and camera."
    )
    user = f"Story:\n{story}\n\n"
    if cast_sheet:
        user += f"{cast_sheet}\n\n"
    user += f"Create up to {max_frames} frames that form one coherent film."
    raw = await chat(system, user, temperature=0.3)
    data = _extract_json(raw)
    if not isinstance(data, list):
        raise ValueError("storyboard response was not a list")
    frames = []
    for i, item in enumerate(data[:max_frames]):
        frames.append(
            {
                "position": i,
                "description": str(item.get("description") or ""),
                "visual_prompt": str(
                    item.get("visual_prompt") or item.get("description") or ""
                ),
                "duration_hint_sec": float(item.get("duration_hint_sec") or 4.0),
                "is_new_shot": bool(item.get("is_new_shot", True)),
            }
        )
    return frames


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
        "You write ONE photorealistic cinematic still image prompt for a moment in a continuous shot. "
        'Return ONLY valid JSON: {"image_prompt": "..."}. '
        "Rules: one moment; concrete subject/pose/camera/light; no collage or panels; "
        "no whole-film dump; never invent subjects not in the beat; do not say the word keyframe. "
        "If a cast lock is provided, you MUST name those characters and weave their exact visual "
        "looks (face, hair, wardrobe) into the image_prompt — do not drop or rewrite the look. "
        "If CONTINUATION: do not say new shot; keep identity from context. "
        "If NEW SHOT and role is first: establish a fresh camera/composition."
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
    )
    if cast_sheet:
        user += f"{cast_sheet}\n"
    if prev_prompt:
        user += f"Previous frame prompt (edit starts from that image):\n{prev_prompt}\n"
    if first_prompt and role != "first":
        user += f"Shot first prompt:\n{first_prompt}\n"
    if last_goal and role != "last":
        user += f"Shot should end toward:\n{last_goal}\n"
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
    return prompt


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
