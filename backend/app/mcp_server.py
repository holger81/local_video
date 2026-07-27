"""
FastMCP server exposing Local Video Studio tools for other LLMs.

Run: python -m app.mcp_server
SSE endpoint (when using mcp SSE transport via uvicorn wrapper): see docker-compose.
"""

from mcp.server.fastmcp import FastMCP

from app.config import get_settings
from app.db.models import init_db
from app.services import movie as movie_svc
from app.services import projects as projects_svc
from app.services import story as story_svc
from app.services import storyboard as sb_svc
from app.services.workflows import list_workflows

mcp = FastMCP("local_video")


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
    """Get a project including storyboard frames."""
    return projects_svc.get_project(project_id)


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
async def propose_storyboard(project_id: int, max_frames: int = 8) -> list:
    """Propose storyboard frames from the current story."""
    return await sb_svc.propose_storyboard(project_id, max_frames)


@mcp.tool()
def update_frame(
    project_id: int,
    frame_id: int,
    description: str | None = None,
    visual_prompt: str | None = None,
    duration_hint_sec: float | None = None,
    is_new_shot: bool | None = None,
) -> dict:
    """Update a storyboard frame."""
    fields = {
        "description": description,
        "visual_prompt": visual_prompt,
        "duration_hint_sec": duration_hint_sec,
        "is_new_shot": is_new_shot,
    }
    return sb_svc.update_frame(project_id, frame_id, **{k: v for k, v in fields.items() if v is not None})


@mcp.tool()
def approve_storyboard(project_id: int) -> dict:
    """Mark the storyboard as approved."""
    return sb_svc.approve_storyboard(project_id)


@mcp.tool()
async def generate_frame_visual(
    project_id: int,
    frame_id: int,
    kind: str = "still",
    workflow_id: str | None = None,
    num_frames: int = 33,
) -> dict:
    """Generate a still or short preview clip for a storyboard frame via ComfyUI."""
    return await sb_svc.generate_frame_visual(
        project_id, frame_id, kind=kind, workflow_id=workflow_id, num_frames=num_frames
    )


@mcp.tool()
async def create_all_stills(
    project_id: int,
    workflow_id: str | None = None,
    skip_existing: bool = False,
) -> dict:
    """Generate stills for every storyboard frame (sequential)."""
    return await sb_svc.generate_all_stills(
        project_id, workflow_id=workflow_id, skip_existing=skip_existing
    )


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
    chunk_frames: int = 33,
    overlap_frames: int = 12,
    t2v_workflow: str = "wan22_t2v",
    i2v_workflow: str = "wan22_i2v",
    seed: int = 42,
) -> dict:
    """
    Start the agentic movie render. Returns immediately with job_id.
    Poll get_job_status until completed/failed.
    """
    return await movie_svc.start_movie(
        project_id,
        target_length_sec=target_length_sec,
        format=format,
        aspect=aspect,
        chunk_frames=chunk_frames,
        overlap_frames=overlap_frames,
        t2v_workflow=t2v_workflow,
        i2v_workflow=i2v_workflow,
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
def list_assets(project_id: int) -> dict:
    """List media assets for a project."""
    return movie_svc.list_assets(project_id)


@mcp.tool()
def get_movie(job_id: int) -> dict:
    """Get final movie path for a completed job."""
    return movie_svc.get_movie(job_id)


def main() -> None:
    init_db()
    settings = get_settings()
    mcp.settings.host = settings.mcp_host
    mcp.settings.port = settings.mcp_port
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
