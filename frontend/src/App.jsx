import { Link, Route, Routes, useParams, useNavigate } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";

const api = async (path, opts = {}) => {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!res.ok) throw new Error(data?.detail || data?.error || res.statusText);
  return data;
};

/** Map container paths like /media/projects/... to the public media API. */
function mediaUrl(absPath) {
  if (!absPath) return null;
  const markers = ["/media/", "/data/"];
  for (const marker of markers) {
    const idx = absPath.indexOf(marker);
    if (idx >= 0) {
      const rel = absPath.slice(idx + marker.length);
      return marker === "/media/" ? `/api/media/${rel}` : null;
    }
  }
  if (!absPath.startsWith("/")) return `/api/media/${absPath}`;
  return null;
}

function Shell({ children }) {
  return (
    <div className="app">
      <header className="top">
        <Link to="/" className="brand">
          Local Video Studio
        </Link>
        <nav>
          <Link to="/">Projects</Link>
        </nav>
      </header>
      <main>{children}</main>
    </div>
  );
}

function Home() {
  const [projects, setProjects] = useState([]);
  const [title, setTitle] = useState("");
  const [genre, setGenre] = useState("");
  const [premise, setPremise] = useState("");
  const [err, setErr] = useState("");
  const nav = useNavigate();

  const load = useCallback(() => {
    api("/projects")
      .then(setProjects)
      .catch((e) => setErr(String(e.message || e)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const create = async (e) => {
    e.preventDefault();
    setErr("");
    try {
      const p = await api("/projects", {
        method: "POST",
        body: JSON.stringify({ title, genre, premise }),
      });
      nav(`/projects/${p.id}`);
    } catch (ex) {
      setErr(String(ex.message || ex));
    }
  };

  return (
    <Shell>
      <section className="hero-panel">
        <h1>Make movies on your LAN</h1>
        <p>
          Storyboard with llama.cpp, generate chunks with ComfyUI Wan 2.2, stitch
          continuous shots locally.
        </p>
      </section>

      <section className="grid two">
        <form className="card-like" onSubmit={create}>
          <h2>New project</h2>
          <label>
            Title
            <input value={title} onChange={(e) => setTitle(e.target.value)} required />
          </label>
          <label>
            Genre
            <input value={genre} onChange={(e) => setGenre(e.target.value)} />
          </label>
          <label>
            Premise
            <textarea value={premise} onChange={(e) => setPremise(e.target.value)} rows={4} />
          </label>
          <button type="submit">Create</button>
          {err && <p className="error">{err}</p>}
        </form>

        <div className="card-like">
          <h2>Projects</h2>
          <ul className="list">
            {projects.map((p) => (
              <li key={p.id}>
                <Link to={`/projects/${p.id}`}>
                  <strong>{p.title}</strong>
                  <span>{p.genre || "—"}</span>
                </Link>
              </li>
            ))}
            {!projects.length && <li className="muted">No projects yet</li>}
          </ul>
        </div>
      </section>
    </Shell>
  );
}

function ProjectPage() {
  const { id } = useParams();
  const [project, setProject] = useState(null);
  const [storyEdit, setStoryEdit] = useState("");
  const [extendInstr, setExtendInstr] = useState("");
  const [busy, setBusy] = useState("");
  const [visualBusy, setVisualBusy] = useState(null);
  const [lightbox, setLightbox] = useState(null);
  const [editDrafts, setEditDrafts] = useState({});
  const [err, setErr] = useState("");
  const [job, setJob] = useState(null);
  const [movieForm, setMovieForm] = useState({
    target_length_sec: 20,
    chunk_frames: 33,
    overlap_frames: 12,
    format: "mp4",
    aspect: "16:9",
    seed: 42,
  });

  const load = useCallback(async () => {
    const p = await api(`/projects/${id}`);
    setProject(p);
    setStoryEdit(p.story || "");
    return p;
  }, [id]);

  useEffect(() => {
    load().catch((e) => setErr(String(e.message || e)));
  }, [load]);

  useEffect(() => {
    if (!job || ["completed", "failed", "cancelled"].includes(job.status)) return;
    const t = setInterval(() => {
      api(`/jobs/${job.id}`)
        .then(setJob)
        .catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, [job]);

  useEffect(() => {
    if (!lightbox) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setLightbox(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox]);

  if (!project) {
    return (
      <Shell>
        <p>{err || "Loading…"}</p>
      </Shell>
    );
  }

  const run = async (label, fn) => {
    setBusy(label);
    setErr("");
    try {
      await fn();
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy("");
    }
  };

  const generateVisual = async (frameId, kind) => {
    setBusy(kind === "still" ? `still ${frameId}` : `preview ${frameId}`);
    setVisualBusy({ frameId, kind });
    setErr("");
    try {
      await api(`/projects/${id}/storyboard/frames/${frameId}/visual`, {
        method: "POST",
        body: JSON.stringify({ kind, num_frames: 33 }),
      });
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setVisualBusy(null);
      setBusy("");
    }
  };

  const editStill = async (frameId) => {
    const instruction = (editDrafts[frameId] || "").trim();
    if (!instruction) {
      setErr("Enter an edit instruction for the still first.");
      return;
    }
    setBusy(`edit still ${frameId}`);
    setVisualBusy({ frameId, kind: "edit" });
    setErr("");
    try {
      await api(`/projects/${id}/storyboard/frames/${frameId}/still/edit`, {
        method: "POST",
        body: JSON.stringify({ instruction }),
      });
      setEditDrafts((prev) => ({ ...prev, [frameId]: "" }));
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setVisualBusy(null);
      setBusy("");
    }
  };

  const createAllStills = async () => {
    const frames = [...(project.frames || [])]
      .sort((a, b) => a.position - b.position)
      .filter((f) => !f.still_path);
    if (!frames.length) {
      setErr("");
      setBusy("");
      return;
    }
    setBusy("create missing stills");
    setErr("");
    try {
      for (let i = 0; i < frames.length; i++) {
        const f = frames[i];
        setBusy(`create missing stills (${i + 1}/${frames.length})`);
        setVisualBusy({ frameId: f.id, kind: "still" });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await api(`/projects/${id}/storyboard/frames/${f.id}/visual`, {
          method: "POST",
          body: JSON.stringify({ kind: "still" }),
        });
        setVisualBusy(null);
        await load();
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setVisualBusy(null);
      setBusy("");
    }
  };

  const deleteMedia = async (frameId, kind, e) => {
    e.stopPropagation();
    setErr("");
    try {
      await api(`/projects/${id}/storyboard/frames/${frameId}/media/${kind}`, {
        method: "DELETE",
      });
      if (lightbox?.frameId === frameId && lightbox?.kind === kind) {
        setLightbox(null);
      }
      await load();
    } catch (ex) {
      setErr(String(ex.message || ex));
    }
  };

  return (
    <Shell>
      <div className="project-head">
        <h1>{project.title}</h1>
        <p className="muted">
          {project.genre} · story {project.story_approved ? "approved" : "draft"} · board{" "}
          {project.storyboard_approved ? "approved" : "open"}
        </p>
      </div>
      {err && <p className="error">{err}</p>}
      {busy && <p className="muted">Working: {busy}…</p>}

      <section className="card-like">
        <h2>1. Story</h2>
        <div className="row">
          <button
            type="button"
            disabled={!!busy}
            onClick={() => run("generate story", () => api(`/projects/${id}/story/generate`, { method: "POST" }))}
          >
            Generate
          </button>
          <button
            type="button"
            disabled={!!busy || !extendInstr}
            onClick={() =>
              run("extend story", () =>
                api(`/projects/${id}/story/extend`, {
                  method: "POST",
                  body: JSON.stringify({ instruction: extendInstr }),
                })
              )
            }
          >
            Extend
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("save story", () =>
                api(`/projects/${id}/story`, {
                  method: "PUT",
                  body: JSON.stringify({ story: storyEdit }),
                })
              )
            }
          >
            Save
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={() => run("approve story", () => api(`/projects/${id}/story/approve`, { method: "POST" }))}
          >
            Approve
          </button>
        </div>
        <input
          placeholder="Extension instruction…"
          value={extendInstr}
          onChange={(e) => setExtendInstr(e.target.value)}
        />
        <textarea rows={10} value={storyEdit} onChange={(e) => setStoryEdit(e.target.value)} />
      </section>

      <section className="card-like">
        <h2>2. Storyboard</h2>
        <div className="row">
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("propose storyboard", () =>
                api(`/projects/${id}/storyboard/propose`, {
                  method: "POST",
                  body: JSON.stringify({ max_frames: 8 }),
                })
              )
            }
          >
            Propose frames
          </button>
          <button
            type="button"
            disabled={
              !!busy || !(project.frames || []).some((f) => !f.still_path)
            }
            onClick={() => createAllStills()}
            title="Generate stills only for frames that do not have one yet"
          >
            Create missing stills
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("approve board", () => api(`/projects/${id}/storyboard/approve`, { method: "POST" }))
            }
          >
            Approve board
          </button>
        </div>
        <div className="frames">
          {(project.frames || []).map((f) => (
            <article key={f.id} className="frame">
              <header>
                <span>#{f.position + 1}</span>
                <span className="tag">{f.is_new_shot ? "new shot" : "continue"}</span>
              </header>
              <textarea
                rows={3}
                defaultValue={f.visual_prompt}
                onBlur={(e) =>
                  api(`/projects/${id}/storyboard/frames/${f.id}`, {
                    method: "PATCH",
                    body: JSON.stringify({ visual_prompt: e.target.value }),
                  }).then(load)
                }
              />
              <div className="row">
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={() => generateVisual(f.id, "still")}
                >
                  Still
                </button>
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={() => generateVisual(f.id, "preview")}
                >
                  Preview clip
                </button>
              </div>
              {mediaUrl(f.still_path) && (
                <div className="still-edit">
                  <input
                    className="still-edit-input"
                    placeholder="Edit still… e.g. make the dress red"
                    value={editDrafts[f.id] || ""}
                    disabled={!!busy}
                    onChange={(e) =>
                      setEditDrafts((prev) => ({ ...prev, [f.id]: e.target.value }))
                    }
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        editStill(f.id);
                      }
                    }}
                  />
                  <button
                    type="button"
                    disabled={!!busy || !(editDrafts[f.id] || "").trim()}
                    onClick={() => editStill(f.id)}
                    title="Modify the existing still with this instruction"
                  >
                    Edit still
                  </button>
                </div>
              )}
              <div className="media-slot">
                {mediaUrl(f.still_path) && (
                  <div className="media-item">
                    <button
                      type="button"
                      className="media-open"
                      onClick={() =>
                        setLightbox({
                          frameId: f.id,
                          kind: "still",
                          src: `${mediaUrl(f.still_path)}?t=${encodeURIComponent(f.still_path)}`,
                          label: `Frame ${f.position + 1} still`,
                        })
                      }
                    >
                      <img
                        className="thumb"
                        src={`${mediaUrl(f.still_path)}?t=${encodeURIComponent(f.still_path)}`}
                        alt={`Frame ${f.position + 1} still`}
                      />
                    </button>
                    <button
                      type="button"
                      className="media-delete"
                      aria-label="Delete still"
                      onClick={(e) => deleteMedia(f.id, "still", e)}
                    >
                      ×
                    </button>
                  </div>
                )}
                {mediaUrl(f.preview_path) && (
                  <div className="media-item">
                    <button
                      type="button"
                      className="media-open"
                      onClick={() =>
                        setLightbox({
                          frameId: f.id,
                          kind: "preview",
                          src: `${mediaUrl(f.preview_path)}?t=${encodeURIComponent(f.preview_path)}`,
                          label: `Frame ${f.position + 1} preview`,
                        })
                      }
                    >
                      <video
                        className="thumb"
                        src={`${mediaUrl(f.preview_path)}?t=${encodeURIComponent(f.preview_path)}`}
                        muted
                        playsInline
                        preload="metadata"
                      />
                    </button>
                    <button
                      type="button"
                      className="media-delete"
                      aria-label="Delete preview"
                      onClick={(e) => deleteMedia(f.id, "preview", e)}
                    >
                      ×
                    </button>
                  </div>
                )}
                {visualBusy?.frameId === f.id && (
                  <div className="thumb-overlay" aria-busy="true">
                    <span className="spinner" />
                    <span className="tiny">
                      {visualBusy.kind === "preview"
                        ? "Creating preview…"
                        : visualBusy.kind === "edit"
                          ? "Editing still…"
                          : "Creating still…"}
                    </span>
                  </div>
                )}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="card-like">
        <h2>3. Movie wizard</h2>
        <div className="grid three">
          <label>
            Length (sec)
            <input
              type="number"
              value={movieForm.target_length_sec}
              onChange={(e) =>
                setMovieForm({ ...movieForm, target_length_sec: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Chunk frames (4n+1)
            <input
              type="number"
              value={movieForm.chunk_frames}
              onChange={(e) => setMovieForm({ ...movieForm, chunk_frames: Number(e.target.value) })}
            />
          </label>
          <label>
            Overlap
            <input
              type="number"
              value={movieForm.overlap_frames}
              onChange={(e) =>
                setMovieForm({ ...movieForm, overlap_frames: Number(e.target.value) })
              }
            />
          </label>
          <label>
            Format
            <input
              value={movieForm.format}
              onChange={(e) => setMovieForm({ ...movieForm, format: e.target.value })}
            />
          </label>
          <label>
            Aspect
            <input
              value={movieForm.aspect}
              onChange={(e) => setMovieForm({ ...movieForm, aspect: e.target.value })}
            />
          </label>
          <label>
            Seed
            <input
              type="number"
              value={movieForm.seed}
              onChange={(e) => setMovieForm({ ...movieForm, seed: Number(e.target.value) })}
            />
          </label>
        </div>
        <div className="row">
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("start movie", async () => {
                const j = await api(`/projects/${id}/movies`, {
                  method: "POST",
                  body: JSON.stringify(movieForm),
                });
                setJob(j);
              })
            }
          >
            Start movie
          </button>
          {job && (
            <>
              <button type="button" onClick={() => api(`/jobs/${job.id}/pause`, { method: "POST" }).then(setJob)}>
                Pause
              </button>
              <button
                type="button"
                onClick={() => api(`/jobs/${job.id}/resume`, { method: "POST" }).then(setJob)}
              >
                Resume
              </button>
              <button
                type="button"
                onClick={() => api(`/jobs/${job.id}/cancel`, { method: "POST" }).then(setJob)}
              >
                Cancel
              </button>
            </>
          )}
        </div>

        {job && (
          <div className="job">
            <h3>
              Job #{job.id} · {job.status}
            </h3>
            <pre>{JSON.stringify(job.progress, null, 2)}</pre>
            <div className="timeline">
              {(job.shots || []).map((s) => (
                <div key={s.id} className="shot">
                  <strong>
                    {s.title} ({s.status})
                  </strong>
                  <ul>
                    {(s.chunks || []).map((c) => (
                      <li key={c.id}>
                        chunk {c.chunk_index} · {c.mode} · {c.status}
                        {c.error ? ` — ${c.error}` : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
            {job.movie_path && <p className="ok">Movie: {job.movie_path}</p>}
            {job.error && <p className="error">{job.error}</p>}
          </div>
        )}
      </section>
      {lightbox && (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          aria-label={lightbox.label}
          onClick={() => setLightbox(null)}
        >
          <div className="lightbox-inner" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="lightbox-close"
              aria-label="Close"
              onClick={() => setLightbox(null)}
            >
              ×
            </button>
            {lightbox.kind === "preview" ? (
              <video src={lightbox.src} controls autoPlay muted playsInline />
            ) : (
              <img src={lightbox.src} alt={lightbox.label} />
            )}
            <p className="muted tiny">{lightbox.label}</p>
          </div>
        </div>
      )}
    </Shell>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/projects/:id" element={<ProjectPage />} />
    </Routes>
  );
}
