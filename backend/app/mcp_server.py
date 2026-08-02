"""
FastMCP server exposing Local Video Studio tools for other LLMs.

Run: python -m app.mcp_server
SSE endpoint: http://<host>:8700/sse (see docker-compose).
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from app.db.models import init_db
from app.services import characters as char_svc
from app.services import images as images_svc
from app.services import library as lib_svc
from app.services import movie as movie_svc
from app.services import projects as projects_svc
from app.services import runtime_settings as rs
from app.services import story as story_svc
from app.services import storyboard as sb_svc
from app.services.comfyui import ComfyUIClient
from app.services.workflows import list_workflows

mcp = FastMCP("local_video")


# --- Projects -----------------------------------------------------------------


@mcp.tool()
def list_projects() -> list:
    """List all video projects."""
    return projects_svc.list_projects()


@mcp.tool()
def create_project(title: str, genre: str = "", premise: str = "") -> dict:
    """Create a new video project with title, optional genre and premise."""
    return projects_svc.create_project(title, genre, premise)


@mcp.tool()
def get_project(project_id: int) -> dict:
    """Get a project including storyboard frames and cast summary fields."""
    return projects_svc.get_project(project_id)


@mcp.tool()
def update_project(
    project_id: int,
    title: str | None = None,
    genre: str | None = None,
    visual_style: str | None = None,
    premise: str | None = None,
    video_backend: str | None = None,
) -> dict:
    """Patch project fields (title, genre, visual_style, premise, video_backend)."""
    fields = {
        "title": title,
        "genre": genre,
        "visual_style": visual_style,
        "premise": premise,
        "video_backend": video_backend,
    }
    return projects_svc.update_project(
        project_id, **{k: v for k, v in fields.items() if v is not None}
    )


# --- Story --------------------------------------------------------------------


@mcp.tool()
async def generate_story(project_id: int) -> dict:
    """Generate a story draft for the project via llama.cpp."""
    return await story_svc.generate_story(project_id)


@mcp.tool()
async def extend_story(project_id: int, instruction: str) -> dict:
    """Extend or revise the project story with an instruction."""
    return await story_svc.extend_story(project_id, instruction)


@mcp.tool()
def set_story(project_id: int, story: str, approved: bool = False) -> dict:
    """Replace the project story text."""
    return story_svc.set_story(project_id, story, approved)


@mcp.tool()
async def approve_story(project_id: int) -> dict:
    """Mark the project story as approved (may trigger cast detect)."""
    return await story_svc.approve_story(project_id)


# --- Characters / outfits -----------------------------------------------------


@mcp.tool()
def list_characters(project_id: int) -> list:
    """List cast characters for a project (appearance, outfits, reference paths)."""
    return char_svc.list_characters(project_id)


@mcp.tool()
def create_character(
    project_id: int,
    name: str,
    description: str = "",
    appearance_prompt: str = "",
    aliases: list[str] | None = None,
    outfits: list[dict[str, Any]] | None = None,
    approved: bool = False,
    intro_frame_id: int | None = None,
) -> dict:
    """Create a cast character. outfits items: {id?, name, prompt, reference_image_path?, is_default?}."""
    return char_svc.create_character(
        project_id,
        name=name,
        description=description,
        appearance_prompt=appearance_prompt,
        aliases=aliases,
        outfits=outfits,
        approved=approved,
        intro_frame_id=intro_frame_id,
        auto_detected=False,
    )


@mcp.tool()
def update_character(
    project_id: int,
    character_id: int,
    name: str | None = None,
    description: str | None = None,
    appearance_prompt: str | None = None,
    aliases: list[str] | None = None,
    outfits: list[dict[str, Any]] | None = None,
    position: int | None = None,
    approved: bool | None = None,
    intro_frame_id: int | None = None,
    reference_image_path: str | None = None,
) -> dict:
    """Patch a character (including outfits list or reference_image_path)."""
    fields = {
        "name": name,
        "description": description,
        "appearance_prompt": appearance_prompt,
        "aliases": aliases,
        "outfits": outfits,
        "position": position,
        "approved": approved,
        "intro_frame_id": intro_frame_id,
        "reference_image_path": reference_image_path,
    }
    return char_svc.update_character(
        project_id, character_id, **{k: v for k, v in fields.items() if v is not None}
    )


@mcp.tool()
def delete_character(project_id: int, character_id: int) -> dict:
    """Delete a character and its reference media."""
    return char_svc.delete_character(project_id, character_id)


@mcp.tool()
async def detect_characters(project_id: int, replace_auto: bool = False) -> list:
    """LLM-extract cast from the story into character rows."""
    return await char_svc.detect_characters(project_id, replace_auto=replace_auto)


@mcp.tool()
async def generate_character_reference(
    project_id: int,
    character_id: int,
    instruction: str | None = None,
) -> dict:
    """Generate or edit the character portrait reference (also syncs a portrait outfit)."""
    return await char_svc.generate_reference(
        project_id, character_id, instruction=instruction
    )


@mcp.tool()
def delete_character_reference(project_id: int, character_id: int) -> dict:
    """Clear the character portrait reference image."""
    return char_svc.delete_reference(project_id, character_id)


@mcp.tool()
async def generate_outfit_reference(
    project_id: int,
    character_id: int,
    outfit_id: str,
    instruction: str | None = None,
) -> dict:
    """Generate or edit a wardrobe reference still for one outfit id."""
    return await char_svc.generate_outfit_reference(
        project_id, character_id, outfit_id, instruction=instruction
    )


@mcp.tool()
def set_character_reference_from_media(
    project_id: int, character_id: int, media_path: str
) -> dict:
    """Copy a library/media image onto a character as their portrait reference."""
    return char_svc.set_character_reference_from_media(
        project_id, character_id, media_path
    )


@mcp.tool()
def set_outfit_reference_from_media(
    project_id: int, character_id: int, outfit_id: str, media_path: str
) -> dict:
    """Copy a library/media image onto an outfit wardrobe still."""
    return char_svc.set_outfit_reference_from_media(
        project_id, character_id, outfit_id, media_path
    )


# --- Scenery / locations ------------------------------------------------------


@mcp.tool()
def list_scenery(project_id: int) -> list:
    """List scenery/locations for a project (appearance, variants, reference paths)."""
    from app.services import scenery as scenery_svc

    return scenery_svc.list_scenery(project_id)


@mcp.tool()
def create_scenery(
    project_id: int,
    name: str,
    description: str = "",
    appearance_prompt: str = "",
    aliases: list[str] | None = None,
    variants: list[dict[str, Any]] | None = None,
    approved: bool = False,
) -> dict:
    """Create a location (e.g. 'the farm', 'inside the barn'). variants: {id?, name, prompt, reference_image_path?, is_default?}."""
    from app.services import scenery as scenery_svc

    return scenery_svc.create_scenery(
        project_id,
        name=name,
        description=description,
        appearance_prompt=appearance_prompt,
        aliases=aliases,
        variants=variants,
        approved=approved,
    )


@mcp.tool()
def update_scenery(
    project_id: int,
    scenery_id: int,
    name: str | None = None,
    description: str | None = None,
    appearance_prompt: str | None = None,
    aliases: list[str] | None = None,
    variants: list[dict[str, Any]] | None = None,
    position: int | None = None,
    approved: bool | None = None,
    reference_image_path: str | None = None,
) -> dict:
    """Patch a scenery row (including variants or reference_image_path)."""
    from app.services import scenery as scenery_svc

    fields = {
        "name": name,
        "description": description,
        "appearance_prompt": appearance_prompt,
        "aliases": aliases,
        "variants": variants,
        "position": position,
        "approved": approved,
        "reference_image_path": reference_image_path,
    }
    return scenery_svc.update_scenery(
        project_id, scenery_id, **{k: v for k, v in fields.items() if v is not None}
    )


@mcp.tool()
def delete_scenery(project_id: int, scenery_id: int) -> dict:
    """Delete a scenery location and its reference media."""
    from app.services import scenery as scenery_svc

    return scenery_svc.delete_scenery(project_id, scenery_id)


@mcp.tool()
async def generate_scenery_reference(
    project_id: int,
    scenery_id: int,
    instruction: str | None = None,
) -> dict:
    """Generate or edit the empty establishing still for a location."""
    from app.services import scenery as scenery_svc

    return await scenery_svc.generate_reference(
        project_id, scenery_id, instruction=instruction
    )


@mcp.tool()
def delete_scenery_reference(project_id: int, scenery_id: int) -> dict:
    """Clear the scenery establishing reference image."""
    from app.services import scenery as scenery_svc

    return scenery_svc.delete_reference(project_id, scenery_id)


@mcp.tool()
def set_scenery_reference_from_media(
    project_id: int, scenery_id: int, media_path: str
) -> dict:
    """Copy a library/media image onto a scenery as its establishing reference."""
    from app.services import scenery as scenery_svc

    return scenery_svc.set_scenery_reference_from_media(
        project_id, scenery_id, media_path
    )


@mcp.tool()
async def generate_scenery_variant_reference(
    project_id: int,
    scenery_id: int,
    variant_id: str,
    instruction: str | None = None,
) -> dict:
    """Generate or edit a scenery variant still (night, interior, etc.)."""
    from app.services import scenery as scenery_svc

    return await scenery_svc.generate_variant_reference(
        project_id, scenery_id, variant_id, instruction=instruction
    )


@mcp.tool()
def set_scenery_variant_reference_from_media(
    project_id: int, scenery_id: int, variant_id: str, media_path: str
) -> dict:
    """Copy a library/media image onto a scenery variant reference."""
    from app.services import scenery as scenery_svc

    return scenery_svc.set_variant_reference_from_media(
        project_id, scenery_id, variant_id, media_path
    )


# --- Storyboard ---------------------------------------------------------------


@mcp.tool()
async def propose_storyboard(
    project_id: int,
    max_frames: int = 8,
    target_duration_sec: float | None = None,
    avg_beat_sec: float | None = None,
    rebuild_prompts: bool = True,
) -> dict:
    """Propose storyboard frames. Does not wipe the board if the LLM fails.

    Prefer target_duration_sec + avg_beat_sec for episode-length boards.
    """
    return await sb_svc.propose_storyboard(
        project_id,
        max_frames,
        target_duration_sec=target_duration_sec,
        avg_beat_sec=avg_beat_sec,
        rebuild_prompts=rebuild_prompts,
    )


@mcp.tool()
async def replace_storyboard(
    project_id: int,
    frames: list[dict[str, Any]],
    rebuild_prompts: bool = False,
) -> dict:
    """Replace the board from a curated JSON frame list (no LLM propose)."""
    return await sb_svc.replace_storyboard_async(
        project_id, frames, rebuild_prompts=rebuild_prompts
    )


@mcp.tool()
def list_frames(project_id: int) -> list:
    """List storyboard frames without dumping the full project."""
    return sb_svc.list_frames(project_id)


@mcp.tool()
def get_frame(project_id: int, frame_id: int) -> dict:
    """Get one storyboard frame."""
    return sb_svc.get_frame(project_id, frame_id)


@mcp.tool()
def create_frame(
    project_id: int,
    description: str = "",
    visual_prompt: str = "",
    dialog: str = "",
    audio_notes: str = "",
    duration_hint_sec: float = 4.0,
    is_new_shot: bool = True,
    position: int | None = None,
    cast: list[dict[str, Any]] | None = None,
) -> dict:
    """Append or insert one storyboard frame without calling the LLM."""
    return sb_svc.create_frame(
        project_id,
        description=description,
        visual_prompt=visual_prompt,
        dialog=dialog,
        audio_notes=audio_notes,
        duration_hint_sec=duration_hint_sec,
        is_new_shot=is_new_shot,
        position=position,
        cast=cast,
    )


@mcp.tool()
def delete_frame(project_id: int, frame_id: int) -> dict:
    """Delete one storyboard frame and re-pack positions."""
    return sb_svc.delete_frame(project_id, frame_id)


@mcp.tool()
def update_frame(
    project_id: int,
    frame_id: int,
    description: str | None = None,
    visual_prompt: str | None = None,
    dialog: str | None = None,
    audio_notes: str | None = None,
    duration_hint_sec: float | None = None,
    is_new_shot: bool | None = None,
    position: int | None = None,
    keyframe_first_prompt: str | None = None,
    keyframe_mid_prompt: str | None = None,
    keyframe_last_prompt: str | None = None,
    keyframes: list[dict[str, Any]] | None = None,
    cast: list[dict[str, Any]] | None = None,
) -> dict:
    """Update a storyboard frame (beat text, dialog, audio_notes, cast, keyframes).

    cast items: {character_id, outfit_id?}.
    keyframes items: {role?, t_sec?, image_prompt|prompt?, path?}.
    """
    fields = {
        "description": description,
        "visual_prompt": visual_prompt,
        "dialog": dialog,
        "audio_notes": audio_notes,
        "duration_hint_sec": duration_hint_sec,
        "is_new_shot": is_new_shot,
        "position": position,
        "keyframe_first_prompt": keyframe_first_prompt,
        "keyframe_mid_prompt": keyframe_mid_prompt,
        "keyframe_last_prompt": keyframe_last_prompt,
        "keyframes": keyframes,
        "cast": cast,
    }
    return sb_svc.update_frame(
        project_id, frame_id, **{k: v for k, v in fields.items() if v is not None}
    )


@mcp.tool()
def approve_storyboard(project_id: int) -> dict:
    """Mark the storyboard as approved."""
    return sb_svc.approve_storyboard(project_id)


@mcp.tool()
async def generate_frame_dialog(
    project_id: int,
    frame_id: int,
    enrich_only: bool | None = None,
) -> dict:
    """LLM-write spoken dialog and/or SFX notes. Enrich-only keeps existing speech."""
    return await sb_svc.generate_frame_dialog(
        project_id, frame_id, enrich_only=enrich_only
    )


@mcp.tool()
async def batch_generate_dialogs(
    project_id: int,
    enrich_only: bool | None = None,
    skip_existing: bool = False,
) -> dict:
    """Generate dialog/audio_notes for every beat."""
    return await sb_svc.generate_all_dialogs(
        project_id, enrich_only=enrich_only, skip_existing=skip_existing
    )

@mcp.tool()
def generate_cast_ref_sheet(project_id: int, frame_id: int) -> dict:
    """Build the labeled cast/outfit contact sheet image for a beat."""
    return sb_svc.generate_cast_ref_sheet(project_id, frame_id)


@mcp.tool()
async def generate_frame_visual(
    project_id: int,
    frame_id: int,
    kind: str = "still",
    workflow_id: str | None = None,
    num_frames: int = 33,
    video_backend: str | None = None,
    fresh: bool = False,
) -> dict:
    """Generate a still or short preview clip for a storyboard frame via ComfyUI.

    fresh=true ignores continuity still and restages from cast refs.
    """
    return await sb_svc.generate_frame_visual(
        project_id,
        frame_id,
        kind=kind,
        workflow_id=workflow_id,
        num_frames=num_frames,
        video_backend=video_backend,
        fresh=fresh,
    )


@mcp.tool()
async def edit_frame_still(
    project_id: int,
    frame_id: int,
    instruction: str,
    workflow_id: str | None = None,
    seed: int | None = None,
) -> dict:
    """Edit an existing storyboard still with a prompt (keeps the image as reference)."""
    return await sb_svc.edit_frame_still(
        project_id,
        frame_id,
        instruction=instruction,
        workflow_id=workflow_id,
        seed=seed,
    )


@mcp.tool()
async def create_all_stills(
    project_id: int,
    workflow_id: str | None = None,
    skip_existing: bool = True,
) -> dict:
    """Generate stills for storyboard frames missing a still (sequential)."""
    return await sb_svc.generate_all_stills(
        project_id, workflow_id=workflow_id, skip_existing=skip_existing
    )


@mcp.tool()
async def rebuild_frame_keyframe_prompts(project_id: int, frame_id: int) -> dict:
    """LLM-plan first/middle(s)/last keyframe image prompts (≤2s spacing)."""
    return await sb_svc.rebuild_frame_keyframe_prompts(project_id, frame_id)


@mcp.tool()
async def generate_one_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    seed: int | None = None,
) -> dict:
    """Render one keyframe (first|mid|last or index) from its stored prompt."""
    return await sb_svc.generate_one_keyframe(project_id, frame_id, phase, seed=seed)


@mcp.tool()
async def edit_frame_keyframe(
    project_id: int,
    frame_id: int,
    phase: str,
    instruction: str,
    seed: int | None = None,
) -> dict:
    """Prompt-edit a keyframe image for one phase."""
    return await sb_svc.edit_frame_keyframe(
        project_id, frame_id, phase, instruction=instruction, seed=seed
    )


@mcp.tool()
async def generate_frame_keyframes(
    project_id: int,
    frame_id: int,
    skip_existing: bool = True,
) -> dict:
    """Create first/mid/last keyframe images for one storyboard step."""
    return await sb_svc.generate_frame_keyframes(
        project_id, frame_id, skip_existing=skip_existing
    )


@mcp.tool()
async def create_all_keyframes(project_id: int, skip_existing: bool = True) -> dict:
    """Blocking: create keyframes for every frame (prefer start_keyframes_job for long boards)."""
    return await sb_svc.generate_all_keyframes(project_id, skip_existing=skip_existing)


@mcp.tool()
async def start_keyframes_job(project_id: int, skip_existing: bool = True) -> dict:
    """Enqueue ARQ keyframe generation; poll get_job_status (kind=keyframes)."""
    from app.services import keyframes_job as kf_job

    return await kf_job.start_keyframes_job(
        project_id, skip_existing=skip_existing
    )


@mcp.tool()
def audit_outfits(project_id: int) -> dict:
    """Flag helmet/spacesuit tokens on summer/everyday outfits."""
    return char_svc.audit_outfits(project_id)


@mcp.tool()
async def generate_step_clips(
    project_id: int,
    frame_id: int,
    num_frames: int = 33,
    workflow_id: str | None = None,
    video_backend: str | None = None,
) -> dict:
    """FLF2V between consecutive keyframes; saves concatenated preview."""
    return await sb_svc.generate_step_clips(
        project_id,
        frame_id,
        num_frames=num_frames,
        workflow_id=workflow_id,
        video_backend=video_backend,
    )


@mcp.tool()
async def create_all_step_clips(
    project_id: int,
    skip_existing: bool = True,
    num_frames: int = 33,
    video_backend: str | None = None,
    workflow_id: str | None = None,
) -> dict:
    """Create within-step clips for every frame that has keyframes."""
    return await sb_svc.generate_all_step_clips(
        project_id,
        skip_existing=skip_existing,
        num_frames=num_frames,
        video_backend=video_backend,
        workflow_id=workflow_id,
    )


@mcp.tool()
async def generate_between_stills(
    project_id: int,
    frame_id: int,
    workflow_id: str | None = None,
    num_frames: int = 33,
    video_backend: str | None = None,
) -> dict:
    """Generate a clip from this frame's end into the next frame's start (FLF2V)."""
    return await sb_svc.generate_between_stills(
        project_id,
        frame_id,
        workflow_id=workflow_id,
        num_frames=num_frames,
        video_backend=video_backend,
    )


@mcp.tool()
async def create_all_between_stills(
    project_id: int,
    workflow_id: str | None = None,
    skip_existing: bool = True,
    num_frames: int = 33,
    video_backend: str | None = None,
) -> dict:
    """Generate between-stills clips for consecutive still pairs (sequential)."""
    return await sb_svc.generate_all_between_stills(
        project_id,
        workflow_id=workflow_id,
        skip_existing=skip_existing,
        num_frames=num_frames,
        video_backend=video_backend,
    )


@mcp.tool()
def delete_frame_media(project_id: int, frame_id: int, kind: str) -> dict:
    """Delete frame media. kind: still|preview|keyframe_first|keyframe_mid|keyframe_last|keyframe:N."""
    return sb_svc.delete_frame_media(project_id, frame_id, kind)


@mcp.tool()
def set_frame_still_from_media(
    project_id: int, frame_id: int, media_path: str
) -> dict:
    """Copy a library/media image onto a frame as its still."""
    return sb_svc.set_frame_still_from_media(project_id, frame_id, media_path)


@mcp.tool()
def set_keyframe_from_media(
    project_id: int,
    frame_id: int,
    media_path: str,
    index: int | None = None,
    role: str | None = None,
) -> dict:
    """Copy a library/media image onto a keyframe slot (index or role=first|middle|last)."""
    return sb_svc.set_keyframe_from_media(
        project_id, frame_id, media_path, index=index, role=role
    )


# --- Image library ------------------------------------------------------------


@mcp.tool()
def upload_library_image(
    image_base64: str, filename: str, label: str | None = None
) -> dict:
    """Upload an image into the global library (base64; optional data: URL prefix)."""
    return lib_svc.upload_image_base64(
        image_base64, filename=filename, label=label
    )


@mcp.tool()
def list_library_images() -> list:
    """List assets in the global image library (id, media_path, url, label)."""
    return lib_svc.list_images()


@mcp.tool()
def get_library_image(asset_id: str) -> dict:
    """Get metadata for one library asset."""
    return lib_svc.get_image(asset_id)


@mcp.tool()
def delete_library_image(asset_id: str) -> dict:
    """Delete a library asset and its files."""
    return lib_svc.delete_image(asset_id)


@mcp.tool()
async def transform_library_image(
    asset_id: str,
    instruction: str,
    seed: int | None = None,
    preserve_style: bool = True,
) -> dict:
    """Edit a library image via still_edit; stores result as a new library asset."""
    return await lib_svc.transform_image(
        asset_id,
        instruction,
        seed=seed,
        preserve_style=preserve_style,
    )


@mcp.tool()
async def apply_project_style_to_image(
    asset_id: str,
    project_id: int,
    instruction: str | None = None,
    seed: int | None = None,
) -> dict:
    """Restyle a library image using the project's visual_style (or genre fallback)."""
    return await lib_svc.apply_project_style(
        asset_id, project_id, instruction=instruction, seed=seed
    )


# --- Generic image ------------------------------------------------------------


@mcp.tool()
async def generate_image(
    prompt: str,
    negative_prompt: str = "",
    seed: int | None = None,
    width: int = 1024,
    height: int = 576,
    steps: int = 20,
    cfg: float = 5.0,
    workflow_id: str | None = None,
    reference_image_path: str | None = None,
    project_id: int | None = None,
    label: str = "gen",
    preserve_style: bool = True,
) -> dict:
    """Generate a generic still via ComfyUI (not tied to a storyboard frame).

    Text-to-image uses still_hero. Pass reference_image_path (under MEDIA_DIR, including
    library/...) for still_edit. Set preserve_style=false to restyle from a reference.
    Returns path, media_path, and /api/media/... url.
    """
    return await images_svc.generate_image(
        prompt,
        negative_prompt=negative_prompt,
        seed=seed,
        width=width,
        height=height,
        steps=steps,
        cfg=cfg,
        workflow_id=workflow_id,
        reference_image_path=reference_image_path,
        project_id=project_id,
        label=label,
        preserve_style=preserve_style,
    )


# --- Movies / jobs ------------------------------------------------------------


@mcp.tool(name="list_workflows")
def list_workflows_tool() -> list:
    """List available ComfyUI workflow profiles."""
    return list_workflows()


@mcp.tool()
async def start_movie(
    project_id: int,
    target_length_sec: float = 30.0,
    format: str = "mp4",
    aspect: str = "16:9",
    chunk_frames: int | None = None,
    overlap_frames: int | None = None,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    video_backend: str | None = None,
    shot_backends: dict[str, str] | None = None,
    t2v_workflow: str | None = None,
    i2v_workflow: str | None = None,
    flf2v_workflow: str | None = None,
    prompt_base: str = "",
    negative_prompt: str = "",
    seed: int = 42,
) -> dict:
    """Start the agentic movie render. Returns immediately with job_id; poll get_job_status."""
    return await movie_svc.start_movie(
        project_id,
        target_length_sec=target_length_sec,
        format=format,
        aspect=aspect,
        chunk_frames=chunk_frames,
        overlap_frames=overlap_frames,
        width=width,
        height=height,
        fps=fps,
        video_backend=video_backend,
        shot_backends=shot_backends,
        t2v_workflow=t2v_workflow,
        i2v_workflow=i2v_workflow,
        flf2v_workflow=flf2v_workflow,
        prompt_base=prompt_base,
        negative_prompt=negative_prompt,
        seed=seed,
    )


@mcp.tool()
def get_job_status(job_id: int) -> dict:
    """Get movie job status including per-shot/chunk progress."""
    return movie_svc.get_job_status(job_id)


@mcp.tool()
def pause_job(job_id: int) -> dict:
    """Pause a running movie job after the current chunk."""
    return movie_svc.pause_job(job_id)


@mcp.tool()
async def resume_job(job_id: int) -> dict:
    """Resume a paused or failed movie job from checkpoint."""
    return await movie_svc.resume_job(job_id)


@mcp.tool()
def cancel_job(job_id: int) -> dict:
    """Cancel a movie job."""
    return movie_svc.cancel_job(job_id)


@mcp.tool()
def delete_job(job_id: int) -> dict:
    """Delete a job record and its movie file when present."""
    return movie_svc.delete_job(job_id)


@mcp.tool()
def list_assets(project_id: int) -> dict:
    """List media assets for a project."""
    return movie_svc.list_assets(project_id)


@mcp.tool()
def get_movie(job_id: int) -> dict:
    """Get final movie path for a completed job."""
    return movie_svc.get_movie(job_id)


# --- Settings / health --------------------------------------------------------


@mcp.tool()
def get_settings_public() -> dict:
    """Return public app settings (Comfy/LLM URLs, defaults; secrets redacted)."""
    return rs.settings_public(get_settings())


@mcp.tool()
async def update_settings(
    llama_base_url: str | None = None,
    llama_model: str | None = None,
    llama_api_key: str | None = None,
    llama_n_ctx: int | None = None,
    llama_max_tokens: int | None = None,
    comfyui_base_url: str | None = None,
    default_video_backend: str | None = None,
) -> dict:
    """Update runtime settings overlay (persisted under data_dir/app_settings.json)."""
    updates = {
        k: v
        for k, v in {
            "llama_base_url": llama_base_url,
            "llama_model": llama_model,
            "llama_api_key": llama_api_key,
            "llama_n_ctx": llama_n_ctx,
            "llama_max_tokens": llama_max_tokens,
            "comfyui_base_url": comfyui_base_url,
            "default_video_backend": default_video_backend,
        }.items()
        if v is not None
    }
    if not updates:
        return rs.settings_public(get_settings())

    if "llama_model" in updates and "llama_n_ctx" not in updates:
        try:
            listed = await list_llm_models()
            match = next(
                (m for m in listed["models"] if m["id"] == updates["llama_model"]),
                None,
            )
            if match and match.get("n_ctx"):
                updates["llama_n_ctx"] = int(match["n_ctx"])
        except Exception:
            pass

    if "default_video_backend" in updates:
        from app.services.video_backends import normalize_backend_id

        updates["default_video_backend"] = normalize_backend_id(
            updates["default_video_backend"]
        )

    rs.save_overlay(updates)
    return rs.settings_public(get_settings())


@mcp.tool()
def list_video_backends() -> dict:
    """List selectable video backends (wan, ltx2, ltx23) and the current default."""
    from app.services.video_backends import list_video_backends as list_backends

    settings = get_settings()
    return {
        "default": settings.default_video_backend or "wan",
        "backends": list_backends(),
    }


@mcp.tool()
async def list_llm_models() -> dict:
    """List models from the configured llama.cpp / OpenAI-compatible server."""
    settings = get_settings()
    base = settings.llama_base_url.rstrip("/")
    url = f"{base}/models"
    headers = {}
    if settings.llama_api_key:
        headers["Authorization"] = f"Bearer {settings.llama_api_key}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise ValueError("Unexpected /models response from LLM server")
    models = [rs.normalize_llm_model(item) for item in raw if isinstance(item, dict)]
    models = [m for m in models if m.get("id")]
    models.sort(
        key=lambda m: (0 if m.get("status") == "loaded" else 1, m["id"].lower())
    )
    return {
        "base_url": settings.llama_base_url,
        "selected": settings.llama_model,
        "llama_n_ctx": settings.llama_n_ctx,
        "llama_max_tokens": settings.llama_max_tokens,
        "models": models,
    }


@mcp.tool()
async def health() -> dict:
    """Check API/MCP process health and ComfyUI reachability."""
    settings = get_settings()
    comfy_ok = False
    comfy_info: Any = None
    try:
        comfy_info = await ComfyUIClient().health()
        comfy_ok = True
    except Exception as e:
        comfy_info = {"error": str(e)}
    return {
        "status": "ok",
        "comfyui": {"ok": comfy_ok, "info": comfy_info},
        "llama_base_url": settings.llama_base_url,
        "llama_model": settings.llama_model,
        "default_video_backend": settings.default_video_backend,
    }


def main() -> None:
    init_db()
    settings = get_settings()
    mcp.settings.host = settings.mcp_host
    mcp.settings.port = settings.mcp_port
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
