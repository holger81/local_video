# Local Video Studio

LAN-hosted studio for storyboarding and long-form video generation. Continuity (shot-aware `4n+1` chunks, overlap discard, lossless PNG handoff, join QA) lives in this tool; ComfyUI runs simple atomic Wan 2.2 workflows.

## Architecture

- **Web UI** + **REST API** (`:8000`)
- **ARQ worker** — durable movie agent (pause / resume / cancel)
- **MCP server** (`:8700` SSE) — same tools for other LLMs
- Talks to your existing **ComfyUI** (`192.168.10.31:8188`) and **llama.cpp** (`192.168.10.31:9292`)

## Portainer / Docker

1. Clone this repo onto the host that runs Portainer (not required to be the GPU box).
2. Copy `.env.example` → `.env` and set URLs if needed.
3. Deploy with Portainer **Stacks** → upload / paste `docker-compose.yml`, or:

```bash
cp .env.example .env
docker compose up -d --build
```

4. Open `http://<host>:8000`
5. MCP SSE: `http://<host>:8700/sse` (see MCP section below)

Volumes on the Portainer host:

```
/shared/local_video/
  data/    # SQLite DB
  media/   # frames, clips, final movies
  redis/   # Redis persistence
```

Create them once if missing: `mkdir -p /shared/local_video/{data,media,redis}`. Workflows still mount from `./comfyui_workflows` in the stack.

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

Tools include: `list_projects`, `create_project`, `generate_story`, `propose_storyboard`, `start_movie`, `get_job_status`, `pause_job`, `resume_job`, `cancel_job`, `get_movie`, …

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
