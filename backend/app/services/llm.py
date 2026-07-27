from __future__ import annotations

import json
import re
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings


def _client() -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(base_url=settings.llama_base_url, api_key=settings.llama_api_key)


async def chat(system: str, user: str, temperature: float = 0.4) -> str:
    settings = get_settings()
    client = _client()
    resp = await client.chat.completions.create(
        model=settings.llama_model,
        temperature=temperature,
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
        "Write a concise story with clear scenes and visual beats. Plain text only."
    )
    user = f"Title: {title}\nGenre: {genre}\nPremise: {premise}\n\nWrite the story."
    return await chat(system, user)


async def extend_story(story: str, instruction: str) -> str:
    system = "You extend film stories while keeping continuity of characters, tone, and setting."
    user = f"Current story:\n{story}\n\nInstruction:\n{instruction}\n\nReturn the full updated story."
    return await chat(system, user)


async def propose_storyboard(story: str, max_frames: int = 8) -> list[dict[str, Any]]:
    system = (
        "You break stories into storyboard frames for video generation. "
        "Return ONLY valid JSON array. Each item: "
        '{"description": str, "visual_prompt": str, "duration_hint_sec": number, "is_new_shot": bool}. '
        "is_new_shot true when camera/scene changes; false for continuous action. "
        "Keep characters, wardrobe, setting, era, and visual style consistent across all frames. "
        "Each visual_prompt should name recurring subjects the same way every time."
    )
    user = f"Story:\n{story}\n\nCreate up to {max_frames} frames that form one coherent film."
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
                "visual_prompt": str(item.get("visual_prompt") or item.get("description") or ""),
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
