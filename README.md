# Local Video Studio

LAN-hosted studio for storyboarding and long-form video generation. Continuity (shot-aware `4n+1` chunks, overlap discard, lossless PNG handoff, join QA) lives in this tool; ComfyUI runs simple atomic Wan 2.2 workflows.

## Architecture

- **Web UI** + **REST API** (`:8000`)
- **ARQ worker** — durable movie agent (pause / resume / cancel)
- **MCP server** (`:8700` SSE) — same tools for other LLMs
- Talks to your existing **ComfyUI** (`192.168.10.31:8188`) and **llama.cpp** (`192.168.10.31:9292`)

## Portainer / Docker

1. Clone this repo onto the host that runs Portainer (not required to be the GPU box).
2. Create storage dirs: `mkdir -p /shared/local_video/{data,media,redis}`
3. Deploy with Portainer **Stacks** (paste/repo `docker-compose.yml`). No `.env` file required — defaults are in compose; override ComfyUI/llama URLs in the stack Environment UI if needed.
4. Or via CLI:

```bash
docker compose up -d --build
```

5. Open `http://<host>:8000`
6. MCP SSE: `http://<host>:8700/sse` (see MCP section below)

### ComfyUI on the AMD box (separate stack)

Studio compose does **not** start ComfyUI. For ROCm, re-apply the SaveVideo audio `.cpu()` patch on every container start via [`docker-compose.comfyui.yml`](docker-compose.comfyui.yml) + [`comfyui/entrypoint-amd.sh`](comfyui/entrypoint-amd.sh).

In Portainer (GPU host), deploy a second stack from the same git repo with compose path `docker-compose.comfyui.yml`, and set `COMFYUI_IMAGE` / `COMFYUI_HOST_DIR` to match your existing install. Or only add the entrypoint mount + `entrypoint` to your current ComfyUI stack.

Volumes on the Portainer host:

```
/shared/local_video/
  data/    # SQLite DB
  media/   # frames, clips, final movies
  redis/   # Redis persistence
```

Create them once if missing: `mkdir -p /shared/local_video/{data,media,redis}`. Workflow JSON ships **inside the Docker image** (no host mount). To customize, copy `comfyui_workflows/` to `/shared/local_video/comfyui_workflows` and uncomment that volume in `docker-compose.yml`.


## ComfyUI workflows

See [comfyui_workflows/README.md](comfyui_workflows/README.md).

Import UI graphs into ComfyUI from `comfyui_workflows/import/`. The app queues `comfyui_workflows/api/` using maps in `maps/`.

Defaults: **33 frames** (`4n+1`), overlap **12**, rolling **last-frame I2V** for continues.

## UI flow

1. Create project → generate / extend / approve story  
2. Propose storyboard → edit prompts → stills / preview clips → approve  
3. Movie wizard (length, chunk frames, overlap) → Start  
4. Watch shot/chunk timeline; pause/resume/cancel; download final MP4 from media path

## MCP (for other LLMs)

Cursor / MCP client example (`mcp.json`):

```json
{
  "mcpServers": {
    "local_video": {
      "url": "http://<host>:8700/sse"
    }
  }
}
```

The MCP surface mirrors the REST API so an agent can drive the full studio:

- **Projects / story:** `list_projects`, `create_project`, `get_project`, `update_project` (incl. `visual_style`), `generate_story`, `extend_story`, `set_story`, `approve_story`
- **Cast:** `list_characters`, `create_character`, `update_character` (incl. `reference_image_path`), `delete_character`, `detect_characters`, `generate_character_reference`, `delete_character_reference`, `generate_outfit_reference`, `set_character_reference_from_media`, `set_outfit_reference_from_media`
- **Storyboard:** `propose_storyboard`, `update_frame` (incl. cast/keyframes/dialog), `approve_storyboard`, `generate_frame_dialog`, `generate_cast_ref_sheet`, stills/keyframes/step-clips/between-stills (+ edit/delete helpers), `set_frame_still_from_media`, `set_keyframe_from_media`
- **Library:** `upload_library_image` (base64), `list_library_images`, `get_library_image`, `delete_library_image`, `transform_library_image`, `apply_project_style_to_image` (uses `visual_style` or falls back to `genre`)
- **Images:** `generate_image` (freeform `still_hero` / `still_edit`; library paths work as `reference_image_path`)
- **Movies:** `start_movie`, `get_job_status`, `pause_job`, `resume_job`, `cancel_job`, `delete_job`, `list_assets`, `get_movie`, `list_workflows`
- **Settings:** `get_settings_public`, `update_settings`, `list_video_backends`, `list_llm_models`, `health`

REST also exposes `POST /api/library/upload`, `GET/DELETE /api/library…`, transform/apply-style routes, and `POST /api/images/generate`. The web UI has a **Library** page plus cast attach controls. Project **Visual style** is editable on the project page.

Long renders return a `job_id` immediately — poll `get_job_status`.

You can also front this through an MCP proxy if you already run one on the LAN.

## Local dev (optional)

```bash
# API
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.
export DATABASE_URL=sqlite:///./local_video.db
export DATA_DIR=../data MEDIA_DIR=../media WORKFLOWS_DIR=../comfyui_workflows
export REDIS_URL=redis://127.0.0.1:6379
uvicorn app.main:app --reload --port 8000

# Worker (separate terminal)
arq app.workers.movie_agent.WorkerSettings

# Frontend
cd frontend && npm install && npm run dev
```

## Continuity notes

See [docs/wan22-continuity.md](docs/wan22-continuity.md).

## License

MIT (unless noted otherwise). Official Wan / ComfyUI templates retain their upstream licenses.
