from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import storyboard as sb_svc

router = APIRouter(prefix="/projects/{project_id}/storyboard", tags=["storyboard"])


class ProposeIn(BaseModel):
    max_frames: int = 8
    target_duration_sec: float | None = None
    avg_beat_sec: float | None = None
    rebuild_prompts: bool = True


class FrameCreate(BaseModel):
    description: str = ""
    visual_prompt: str = ""
    dialog: str = ""
    audio_notes: str = ""
    duration_hint_sec: float = 4.0
    is_new_shot: bool = True
    position: int | None = None
    cast: list | None = None
    scenery: list | None = None


class FrameUpdate(BaseModel):
    description: str | None = None
    visual_prompt: str | None = None
    dialog: str | None = None
    audio_notes: str | None = None
    duration_hint_sec: float | None = None
    is_new_shot: bool | None = None
    position: int | None = None
    keyframe_first_prompt: str | None = None
    keyframe_mid_prompt: str | None = None
    keyframe_last_prompt: str | None = None
    keyframes: list | None = None
    cast: list | None = None
    scenery: list | None = None


class ReplaceBoardIn(BaseModel):
    frames: list[dict]
    rebuild_prompts: bool = False


class DialogBatchIn(BaseModel):
    enrich_only: bool | None = None
    skip_existing: bool = False


class DialogIn(BaseModel):
    enrich_only: bool | None = None


class VisualIn(BaseModel):
    kind: str = "still"
    workflow_id: str | None = None
    num_frames: int = 33
    video_backend: str | None = None
    # When true, ignore existing/previous still and restage from cast refs only.
    fresh: bool = False


class EditStillIn(BaseModel):
    instruction: str
    workflow_id: str | None = None
    seed: int | None = None


class EditKeyframeIn(BaseModel):
    instruction: str
    seed: int | None = None


class KeyframePhaseIn(BaseModel):
    seed: int | None = None


class AllStillsIn(BaseModel):
    workflow_id: str | None = None
    skip_existing: bool = True


class BetweenStillsIn(BaseModel):
    workflow_id: str | None = None
    num_frames: int = 33
    video_backend: str | None = None


class AllBetweenStillsIn(BaseModel):
    workflow_id: str | None = None
    skip_existing: bool = True
    num_frames: int = 33
    video_backend: str | None = None


class KeyframesIn(BaseModel):
    skip_existing: bool = True
    # When true, enqueue ARQ job instead of blocking HTTP.
    as_job: bool = False


class StepClipsIn(BaseModel):
    workflow_id: str | None = None
    num_frames: int = 33
    skip_existing: bool = True
    video_backend: str | None = None


class MediaPathIn(BaseModel):
    media_path: str


class KeyframeFromMediaIn(BaseModel):
    media_path: str
    index: int | None = None
    role: str | None = None


@router.post("/propose")
async def propose(project_id: int, body: ProposeIn | None = None):
    body = body or ProposeIn()
    try:
        return await sb_svc.propose_storyboard(
            project_id,
            body.max_frames,
            target_duration_sec=body.target_duration_sec,
            avg_beat_sec=body.avg_beat_sec,
            rebuild_prompts=body.rebuild_prompts,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.get("/frames")
def list_frames(project_id: int):
    try:
        return sb_svc.list_frames(project_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/frames/{frame_id}")
def get_frame(project_id: int, frame_id: int):
    try:
        return sb_svc.get_frame(project_id, frame_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/frames")
def create_frame(project_id: int, body: FrameCreate):
    try:
        return sb_svc.create_frame(
            project_id, **body.model_dump(exclude_none=True)
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/replace")
async def replace_board(project_id: int, body: ReplaceBoardIn):
    try:
        return await sb_svc.replace_storyboard_async(
            project_id,
            body.frames,
            rebuild_prompts=body.rebuild_prompts,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/frames/{frame_id}")
def delete_frame(project_id: int, frame_id: int):
    try:
        return sb_svc.delete_frame(project_id, frame_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.patch("/frames/{frame_id}")
def update_frame(project_id: int, frame_id: int, body: FrameUpdate):
    try:
        return sb_svc.update_frame(project_id, frame_id, **body.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/approve")
def approve(project_id: int):
    try:
        return sb_svc.approve_storyboard(project_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/stills")
async def create_all_stills(project_id: int, body: AllStillsIn | None = None):
    body = body or AllStillsIn()
    try:
        return await sb_svc.generate_all_stills(
            project_id,
            workflow_id=body.workflow_id,
            skip_existing=body.skip_existing,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/keyframes")
async def create_all_keyframes(project_id: int, body: KeyframesIn | None = None):
    body = body or KeyframesIn()
    try:
        if body.as_job:
            from app.services import keyframes_job as kf_job

            return await kf_job.start_keyframes_job(
                project_id, skip_existing=body.skip_existing
            )
        return await sb_svc.generate_all_keyframes(
            project_id, skip_existing=body.skip_existing
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/keyframes/job")
async def start_keyframes_job(project_id: int, body: KeyframesIn | None = None):
    body = body or KeyframesIn()
    try:
        from app.services import keyframes_job as kf_job

        return await kf_job.start_keyframes_job(
            project_id, skip_existing=body.skip_existing
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/step-clips")
async def create_all_step_clips(project_id: int, body: StepClipsIn | None = None):
    body = body or StepClipsIn()
    try:
        return await sb_svc.generate_all_step_clips(
            project_id,
            skip_existing=body.skip_existing,
            num_frames=body.num_frames,
            workflow_id=body.workflow_id,
            video_backend=body.video_backend,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/between-stills")
async def create_all_between_stills(project_id: int, body: AllBetweenStillsIn | None = None):
    body = body or AllBetweenStillsIn()
    try:
        return await sb_svc.generate_all_between_stills(
            project_id,
            workflow_id=body.workflow_id,
            skip_existing=body.skip_existing,
            num_frames=body.num_frames,
            video_backend=body.video_backend,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/visual")
async def generate_visual(project_id: int, frame_id: int, body: VisualIn | None = None):
    body = body or VisualIn()
    try:
        return await sb_svc.generate_frame_visual(
            project_id,
            frame_id,
            kind=body.kind,
            workflow_id=body.workflow_id,
            num_frames=body.num_frames,
            video_backend=body.video_backend,
            fresh=body.fresh,
        )
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/dialog")
async def generate_frame_dialog(
    project_id: int, frame_id: int, body: DialogIn | None = None
):
    """LLM: fill spoken dialog and/or SFX notes for this beat."""
    body = body or DialogIn()
    try:
        return await sb_svc.generate_frame_dialog(
            project_id, frame_id, enrich_only=body.enrich_only
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/dialogs")
async def generate_all_dialogs(project_id: int, body: DialogBatchIn | None = None):
    body = body or DialogBatchIn()
    try:
        return await sb_svc.generate_all_dialogs(
            project_id,
            enrich_only=body.enrich_only,
            skip_existing=body.skip_existing,
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/keyframes")
async def frame_keyframes(project_id: int, frame_id: int, body: KeyframesIn | None = None):
    body = body or KeyframesIn()
    try:
        return await sb_svc.generate_frame_keyframes(
            project_id, frame_id, skip_existing=body.skip_existing
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


class RebuildPromptsIn(BaseModel):
    spacing_sec: float = 5.0


@router.post("/frames/{frame_id}/keyframes/rebuild-prompts")
async def rebuild_keyframe_prompts(
    project_id: int, frame_id: int, body: RebuildPromptsIn | None = None
):
    body = body or RebuildPromptsIn()
    try:
        return await sb_svc.rebuild_frame_keyframe_prompts(
            project_id, frame_id, spacing_sec=body.spacing_sec
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


# Static path segments before /keyframes/{phase} so "from-media" is not parsed as a phase.
@router.post("/frames/{frame_id}/keyframes/from-media")
def set_keyframe_from_media(project_id: int, frame_id: int, body: KeyframeFromMediaIn):
    try:
        return sb_svc.set_keyframe_from_media(
            project_id,
            frame_id,
            body.media_path,
            index=body.index,
            role=body.role,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/frames/{frame_id}/keyframes/{phase}")
async def frame_one_keyframe(
    project_id: int, frame_id: int, phase: str, body: KeyframePhaseIn | None = None
):
    body = body or KeyframePhaseIn()
    try:
        return await sb_svc.generate_one_keyframe(
            project_id, frame_id, phase, seed=body.seed
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/keyframes/{phase}/edit")
async def edit_keyframe(project_id: int, frame_id: int, phase: str, body: EditKeyframeIn):
    try:
        return await sb_svc.edit_frame_keyframe(
            project_id,
            frame_id,
            phase,
            instruction=body.instruction,
            seed=body.seed,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/step-clips")
async def frame_step_clips(project_id: int, frame_id: int, body: StepClipsIn | None = None):
    body = body or StepClipsIn()
    try:
        return await sb_svc.generate_step_clips(
            project_id,
            frame_id,
            workflow_id=body.workflow_id,
            num_frames=body.num_frames,
            video_backend=body.video_backend,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/between-stills")
async def between_stills(project_id: int, frame_id: int, body: BetweenStillsIn | None = None):
    body = body or BetweenStillsIn()
    try:
        return await sb_svc.generate_between_stills(
            project_id,
            frame_id,
            workflow_id=body.workflow_id,
            num_frames=body.num_frames,
            video_backend=body.video_backend,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/cast-sheet")
def generate_cast_sheet(project_id: int, frame_id: int):
    try:
        return sb_svc.generate_cast_ref_sheet(project_id, frame_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/frames/{frame_id}/still/edit")
async def edit_still(project_id: int, frame_id: int, body: EditStillIn):
    try:
        return await sb_svc.edit_frame_still(
            project_id,
            frame_id,
            instruction=body.instruction,
            workflow_id=body.workflow_id,
            seed=body.seed,
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@router.post("/frames/{frame_id}/still/from-media")
def set_still_from_media(project_id: int, frame_id: int, body: MediaPathIn):
    try:
        return sb_svc.set_frame_still_from_media(
            project_id, frame_id, body.media_path
        )
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/frames/{frame_id}/media/{kind}")
def delete_media(project_id: int, frame_id: int, kind: str):
    try:
        return sb_svc.delete_frame_media(project_id, frame_id, kind)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
