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

function frameKeyframes(f) {
  if (Array.isArray(f?.keyframes) && f.keyframes.length) return f.keyframes;
  const out = [];
  if (f?.keyframe_first_path || f?.keyframe_first_prompt) {
    out.push({
      index: 0,
      t_sec: 0,
      role: "first",
      image_prompt: f.keyframe_first_prompt || "",
      path: f.keyframe_first_path || null,
    });
  }
  if (f?.keyframe_mid_path || f?.keyframe_mid_prompt) {
    out.push({
      index: out.length,
      t_sec: 2,
      role: "middle",
      image_prompt: f.keyframe_mid_prompt || "",
      path: f.keyframe_mid_path || null,
    });
  }
  if (f?.keyframe_last_path || f?.keyframe_last_prompt) {
    out.push({
      index: out.length,
      t_sec: f.duration_hint_sec || 4,
      role: "last",
      image_prompt: f.keyframe_last_prompt || "",
      path: f.keyframe_last_path || null,
    });
  }
  return out;
}

function firstKeyframePath(f) {
  const kfs = frameKeyframes(f);
  return kfs[0]?.path || null;
}

function beatSummary(f) {
  const text = (f.visual_prompt || f.description || "").trim();
  if (!text) return "No beat text yet";
  return text.length > 140 ? `${text.slice(0, 139)}…` : text;
}

function keyframesReady(f) {
  const kfs = frameKeyframes(f);
  return kfs.length >= 2 && kfs.every((k) => !!k.path);
}

function keyframeRoleLabel(role, index, total) {
  if (role === "first") return "Start";
  if (role === "last") return "End";
  return `Mid ${index}`;
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
  const [keyframeEditorId, setKeyframeEditorId] = useState(null);
  const [editorDraft, setEditorDraft] = useState(null);
  const [kfEditDrafts, setKfEditDrafts] = useState({});
  const [err, setErr] = useState("");
  const [job, setJob] = useState(null);
  const [movies, setMovies] = useState([]);
  const [movieForm, setMovieForm] = useState({
    target_length_sec: 20,
    chunk_frames: 33,
    overlap_frames: 12,
    format: "mp4",
    aspect: "16:9",
    seed: 42,
  });

  const loadAssets = useCallback(async () => {
    const a = await api(`/projects/${id}/assets`);
    setMovies((a.movies || []).filter((m) => m.movie_path && m.status === "completed"));
    return a;
  }, [id]);

  const load = useCallback(async () => {
    const p = await api(`/projects/${id}`);
    setProject(p);
    setStoryEdit(p.story || "");
    try {
      await loadAssets();
    } catch {
      /* assets optional */
    }
    return p;
  }, [id, loadAssets]);

  useEffect(() => {
    load().catch((e) => setErr(String(e.message || e)));
  }, [load]);

  // On first load, restore the most recent job so completed movies reappear after refresh.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const a = await api(`/projects/${id}/assets`);
        const list = a.movies || [];
        if (!list.length || cancelled) return;
        setMovies(list.filter((m) => m.movie_path && m.status === "completed"));
        const latestId = list[0].job_id;
        const j = await api(`/jobs/${latestId}`);
        if (!cancelled) setJob(j);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  useEffect(() => {
    if (!job || ["completed", "failed", "cancelled"].includes(job.status)) return;
    const t = setInterval(() => {
      api(`/jobs/${job.id}`)
        .then(async (j) => {
          setJob(j);
          if (j.status === "completed") {
            try {
              await loadAssets();
            } catch {
              /* ignore */
            }
          }
        })
        .catch(() => {});
    }, 3000);
    return () => clearInterval(t);
  }, [job, loadAssets]);

  useEffect(() => {
    if (!lightbox) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setLightbox(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [lightbox]);

  useEffect(() => {
    if (!keyframeEditorId || !project) return;
    const f = (project.frames || []).find((x) => x.id === keyframeEditorId);
    if (!f) {
      setKeyframeEditorId(null);
      setEditorDraft(null);
      return;
    }
    setEditorDraft({
      description: f.description || "",
      visual_prompt: f.visual_prompt || "",
      duration_hint_sec: f.duration_hint_sec ?? 4,
      is_new_shot: !!f.is_new_shot,
      keyframes: frameKeyframes(f).map((k, i) => ({
        index: i,
        t_sec: k.t_sec,
        role: k.role,
        image_prompt: k.image_prompt || "",
        path: k.path || null,
      })),
    });
  }, [keyframeEditorId, project]);

  useEffect(() => {
    if (!keyframeEditorId) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape" && !lightbox) {
        setKeyframeEditorId(null);
        setEditorDraft(null);
        setKfEditDrafts({});
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [keyframeEditorId, lightbox]);

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

  const openKeyframeEditor = (frameId) => {
    setKfEditDrafts({});
    setKeyframeEditorId(frameId);
  };

  const patchEditorFields = async () => {
    if (!keyframeEditorId || !editorDraft) return;
    await api(`/projects/${id}/storyboard/frames/${keyframeEditorId}`, {
      method: "PATCH",
      body: JSON.stringify({
        description: editorDraft.description,
        visual_prompt: editorDraft.visual_prompt,
        duration_hint_sec: Number(editorDraft.duration_hint_sec) || 4,
        is_new_shot: !!editorDraft.is_new_shot,
        keyframes: (editorDraft.keyframes || []).map((k, i) => ({
          index: i,
          t_sec: k.t_sec,
          role: k.role,
          image_prompt: k.image_prompt,
          path: k.path || null,
        })),
      }),
    });
  };

  const saveKeyframeEditor = async () => {
    if (!keyframeEditorId || !editorDraft) return;
    setBusy(`save step ${keyframeEditorId}`);
    setErr("");
    try {
      await patchEditorFields();
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy("");
    }
  };

  const rebuildKeyframePrompts = async () => {
    if (!keyframeEditorId) return;
    await run(`rebuild prompts ${keyframeEditorId}`, async () => {
      await patchEditorFields();
      await api(
        `/projects/${id}/storyboard/frames/${keyframeEditorId}/keyframes/rebuild-prompts`,
        { method: "POST" }
      );
    });
  };

  const renderOneKeyframe = async (phaseOrIndex) => {
    if (!keyframeEditorId) return;
    setBusy(`keyframe ${phaseOrIndex}`);
    setVisualBusy({ frameId: keyframeEditorId, kind: `keyframe_${phaseOrIndex}` });
    setErr("");
    try {
      await patchEditorFields();
      await api(
        `/projects/${id}/storyboard/frames/${keyframeEditorId}/keyframes/${phaseOrIndex}`,
        { method: "POST", body: JSON.stringify({}) }
      );
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setVisualBusy(null);
      setBusy("");
    }
  };

  const editOneKeyframe = async (phaseOrIndex) => {
    if (!keyframeEditorId) return;
    const instruction = (kfEditDrafts[phaseOrIndex] || "").trim();
    if (!instruction) {
      setErr(`Enter an edit instruction for keyframe ${phaseOrIndex}.`);
      return;
    }
    setBusy(`edit keyframe ${phaseOrIndex}`);
    setVisualBusy({ frameId: keyframeEditorId, kind: `keyframe_${phaseOrIndex}` });
    setErr("");
    try {
      await api(
        `/projects/${id}/storyboard/frames/${keyframeEditorId}/keyframes/${phaseOrIndex}/edit`,
        { method: "POST", body: JSON.stringify({ instruction }) }
      );
      setKfEditDrafts((prev) => ({ ...prev, [phaseOrIndex]: "" }));
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setVisualBusy(null);
      setBusy("");
    }
  };

  const createMissingVisuals = async (kind) => {
    const pathKey = kind === "preview" ? "preview_path" : "still_path";
    const label = kind === "preview" ? "previews" : "stills";
    const frames = [...(project.frames || [])]
      .sort((a, b) => a.position - b.position)
      .filter((f) => !f[pathKey]);
    if (!frames.length) {
      setErr("");
      setBusy("");
      return;
    }
    setBusy(`create missing ${label}`);
    setErr("");
    try {
      for (let i = 0; i < frames.length; i++) {
        const f = frames[i];
        setBusy(`create missing ${label} (${i + 1}/${frames.length})`);
        setVisualBusy({ frameId: f.id, kind });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await api(`/projects/${id}/storyboard/frames/${f.id}/visual`, {
          method: "POST",
          body: JSON.stringify({ kind, num_frames: 33 }),
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

  const createBetweenStills = async () => {
    const frames = [...(project.frames || [])].sort((a, b) => a.position - b.position);
    const pairs = [];
    for (let i = 0; i < frames.length - 1; i++) {
      if (frames[i].still_path && frames[i + 1].still_path && !frames[i].preview_path) {
        pairs.push(frames[i]);
      }
    }
    if (!pairs.length) {
      setErr("");
      setBusy("");
      return;
    }
    setBusy("create between-stills");
    setErr("");
    try {
      for (let i = 0; i < pairs.length; i++) {
        const f = pairs[i];
        setBusy(`create between-stills (${i + 1}/${pairs.length})`);
        setVisualBusy({ frameId: f.id, kind: "between" });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await api(`/projects/${id}/storyboard/frames/${f.id}/between-stills`, {
          method: "POST",
          body: JSON.stringify({ num_frames: 33 }),
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

  const createMissingKeyframes = async () => {
    const frames = [...(project.frames || [])]
      .sort((a, b) => a.position - b.position)
      .filter((f) => !keyframesReady(f));
    if (!frames.length) return;
    setBusy("create keyframes");
    setErr("");
    try {
      for (let i = 0; i < frames.length; i++) {
        const f = frames[i];
        setBusy(`create keyframes (${i + 1}/${frames.length})`);
        setVisualBusy({ frameId: f.id, kind: "keyframes" });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await api(`/projects/${id}/storyboard/frames/${f.id}/keyframes`, {
          method: "POST",
          body: JSON.stringify({ skip_existing: true }),
        });
        setVisualBusy(null);
        await load();
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setVisualBusy(null);
      setBusy("");
    }
  };

  const createStepClips = async () => {
    const frames = [...(project.frames || [])]
      .sort((a, b) => a.position - b.position)
      .filter((f) => keyframesReady(f) && !f.preview_path);
    if (!frames.length) return;
    setBusy("create step clips");
    setErr("");
    try {
      for (let i = 0; i < frames.length; i++) {
        const f = frames[i];
        setBusy(`create step clips (${i + 1}/${frames.length})`);
        setVisualBusy({ frameId: f.id, kind: "step_clips" });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        await api(`/projects/${id}/storyboard/frames/${f.id}/step-clips`, {
          method: "POST",
          body: JSON.stringify({ num_frames: 33 }),
        });
        setVisualBusy(null);
        await load();
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
        <p className="muted">
          One thumbnail per beat (first keyframe). Click a card to open the step editor for
          prompts, stills, the full keyframe series, and motion.
        </p>
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
            Propose storyboard beats
          </button>
          <button
            type="button"
            disabled={
              !!busy || !(project.frames || []).some((f) => !f.still_path)
            }
            onClick={() => createMissingVisuals("still")}
            title="Generate a hero still only for beats that do not have one yet"
          >
            Create missing hero stills
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("approve board", () => api(`/projects/${id}/storyboard/approve`, { method: "POST" }))
            }
          >
            Approve storyboard
          </button>
        </div>
        <div className="frames frames-compact">
          {(project.frames || []).map((f) => {
            const kfPath = firstKeyframePath(f);
            const kfBusy =
              visualBusy?.frameId === f.id &&
              (visualBusy.kind === "keyframes" ||
                visualBusy.kind === "keyframe_0" ||
                visualBusy.kind === "keyframe_first");
            return (
              <article key={f.id} className="frame frame-compact">
                <button
                  type="button"
                  className="frame-compact-open"
                  onClick={() => openKeyframeEditor(f.id)}
                  title="Open step editor"
                >
                  <header className="frame-compact-head">
                    <span>#{f.position + 1}</span>
                    <span className="tag">{f.is_new_shot ? "new shot" : "continue"}</span>
                    {keyframesReady(f) && <span className="tag ok-tag">keyframes</span>}
                    {f.preview_path && <span className="tag ok-tag">preview</span>}
                  </header>
                  <div className="frame-compact-media">
                    {mediaUrl(kfPath) ? (
                      <img
                        className="frame-compact-thumb"
                        src={`${mediaUrl(kfPath)}?t=${encodeURIComponent(kfPath)}`}
                        alt={`Step ${f.position + 1} first keyframe`}
                      />
                    ) : (
                      <div
                        className={`thumb-placeholder frame-compact-thumb${kfBusy ? " is-busy" : ""}`}
                      >
                        {kfBusy && <span className="spinner" />}
                      </div>
                    )}
                  </div>
                  <p className="frame-compact-summary muted">{beatSummary(f)}</p>
                  <span className="frame-open-hint">Open step editor</span>
                </button>
              </article>
            );
          })}
        </div>
      </section>

      <section className="card-like">
        <h2>3. Batch keyframes &amp; motion</h2>
        <p className="muted">
          Run across the board: LLM image prompts → edit-chain keyframes (new shot =
          own series; continue = from prior end) → FLF2V animate consecutive pairs → bridge beats.
        </p>
        <div className="frame-stage-actions batch-actions">
          <button
            type="button"
            disabled={!!busy || !(project.frames || []).some((f) => !keyframesReady(f))}
            onClick={() => createMissingKeyframes()}
            title="Create missing images in each beat’s keyframe series"
          >
            Create missing keyframe images
          </button>
          <button
            type="button"
            disabled={
              !!busy ||
              !(project.frames || []).some((f) => keyframesReady(f) && !f.preview_path)
            }
            onClick={() => createStepClips()}
            title="FLF2V animate consecutive keyframes for beats missing a preview"
          >
            Animate beats missing a preview
          </button>
          <button
            type="button"
            disabled={
              !!busy ||
              !(function () {
                const frames = [...(project.frames || [])].sort(
                  (a, b) => a.position - b.position
                );
                for (let i = 0; i < frames.length - 1; i++) {
                  const a = frames[i];
                  const b = frames[i + 1];
                  const aEnd = frameKeyframes(a).slice(-1)[0]?.path || a.still_path;
                  const bStart = frameKeyframes(b)[0]?.path || b.still_path;
                  if (aEnd && bStart) return true;
                }
                return false;
              })()
            }
            onClick={() => createBetweenStills()}
            title="FLF2V bridge clips from each beat’s end into the next beat’s start"
          >
            Bridge clips between beats
          </button>
        </div>
      </section>

      <section className="card-like">
        <h2>4. Movie wizard</h2>
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
            {job.movie_path && mediaUrl(job.movie_path) && (
              <div className="movie-result">
                <video
                  className="movie-player"
                  src={`${mediaUrl(job.movie_path)}?t=${encodeURIComponent(job.movie_path)}`}
                  controls
                  playsInline
                  preload="metadata"
                />
                <div className="row">
                  <a
                    className="linkish"
                    href={mediaUrl(job.movie_path)}
                    download={`project-${id}-job-${job.id}.mp4`}
                  >
                    Download movie
                  </a>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() =>
                      setLightbox({
                        frameId: null,
                        kind: "preview",
                        src: `${mediaUrl(job.movie_path)}?t=${encodeURIComponent(job.movie_path)}`,
                        label: `Job #${job.id} movie`,
                      })
                    }
                  >
                    Enlarge
                  </button>
                </div>
              </div>
            )}
            {job.movie_path && !mediaUrl(job.movie_path) && (
              <p className="ok">Movie saved at: {job.movie_path}</p>
            )}
            {job.error && <p className="error">{job.error}</p>}
          </div>
        )}

        {movies.length > 0 && (
          <div className="movie-library">
            <h3>Completed movies</h3>
            <div className="movie-library-grid">
              {movies.map((m) => {
                const src = mediaUrl(m.movie_path);
                if (!src) {
                  return (
                    <div key={m.job_id} className="movie-card">
                      <p className="muted tiny">
                        Job #{m.job_id} · path not served ({m.movie_path})
                      </p>
                    </div>
                  );
                }
                return (
                  <div key={m.job_id} className="movie-card">
                    <video
                      className="movie-player"
                      src={`${src}?t=${encodeURIComponent(m.movie_path)}`}
                      controls
                      playsInline
                      preload="metadata"
                    />
                    <div className="row movie-card-actions">
                      <span className="tiny muted">Job #{m.job_id}</span>
                      <a
                        className="linkish"
                        href={src}
                        download={`project-${id}-job-${m.job_id}.mp4`}
                      >
                        Download
                      </a>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </section>
      {keyframeEditorId && editorDraft && (() => {
        const f = (project.frames || []).find((x) => x.id === keyframeEditorId);
        if (!f) return null;
        const draftKfs = editorDraft.keyframes || [];
        return (
          <div
            className="kf-editor"
            role="dialog"
            aria-modal="true"
            aria-label={`Keyframe editor step ${f.position + 1}`}
            onClick={() => {
              if (!busy) {
                setKeyframeEditorId(null);
                setEditorDraft(null);
                setKfEditDrafts({});
              }
            }}
          >
            <div className="kf-editor-inner" onClick={(e) => e.stopPropagation()}>
              <header className="kf-editor-head">
                <div>
                  <h2>Step #{f.position + 1} editor</h2>
                  <p className="muted tiny">
                    LLM image prompts only (Comfy never sees the full story). Middles fill
                    ≤2s gaps. {f.is_new_shot ? "New shot = own series." : "Continue = edit from prior end."}
                  </p>
                </div>
                <button
                  type="button"
                  className="lightbox-close"
                  aria-label="Close"
                  disabled={!!busy}
                  onClick={() => {
                    setKeyframeEditorId(null);
                    setEditorDraft(null);
                    setKfEditDrafts({});
                  }}
                >
                  ×
                </button>
              </header>

              <div className="kf-editor-grid">
                <label className="kf-field">
                  <span>Description</span>
                  <textarea
                    rows={3}
                    value={editorDraft.description}
                    disabled={!!busy}
                    onChange={(e) =>
                      setEditorDraft((d) => ({ ...d, description: e.target.value }))
                    }
                  />
                </label>
                <label className="kf-field">
                  <span>Visual prompt</span>
                  <textarea
                    rows={3}
                    value={editorDraft.visual_prompt}
                    disabled={!!busy}
                    onChange={(e) =>
                      setEditorDraft((d) => ({ ...d, visual_prompt: e.target.value }))
                    }
                  />
                </label>
                <div className="kf-field-row">
                  <label className="kf-field">
                    <span>Duration hint (sec)</span>
                    <input
                      type="number"
                      min={0.5}
                      step={0.5}
                      value={editorDraft.duration_hint_sec}
                      disabled={!!busy}
                      onChange={(e) =>
                        setEditorDraft((d) => ({
                          ...d,
                          duration_hint_sec: e.target.value,
                        }))
                      }
                    />
                  </label>
                  <label className="kf-check">
                    <input
                      type="checkbox"
                      checked={!!editorDraft.is_new_shot}
                      disabled={!!busy}
                      onChange={(e) =>
                        setEditorDraft((d) => ({ ...d, is_new_shot: e.target.checked }))
                      }
                    />
                    New shot (own keyframe series)
                  </label>
                </div>
              </div>

              <div className="row kf-editor-actions">
                <button type="button" disabled={!!busy} onClick={saveKeyframeEditor}>
                  Save inputs
                </button>
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={rebuildKeyframePrompts}
                  title="LLM-plan start/middles/end image prompts from beat + duration (≤2s spacing)"
                >
                  Rebuild LLM keyframe prompts
                </button>
                <button
                  type="button"
                  disabled={!!busy}
                  onClick={() =>
                    run(`keyframes ${f.id}`, async () => {
                      setVisualBusy({ frameId: f.id, kind: "keyframes" });
                      try {
                        await patchEditorFields();
                        await api(`/projects/${id}/storyboard/frames/${f.id}/keyframes`, {
                          method: "POST",
                          body: JSON.stringify({ skip_existing: false }),
                        });
                      } finally {
                        setVisualBusy(null);
                      }
                    })
                  }
                >
                  Regenerate all keyframe images
                </button>
              </div>

              <section className="frame-stage kf-editor-section">
                <div className="frame-stage-head">
                  <strong>Hero still</strong>
                  <span className="tiny muted">Optional reference image for this beat</span>
                </div>
                <div className="frame-stage-actions">
                  <button
                    type="button"
                    disabled={!!busy}
                    onClick={() => generateVisual(f.id, "still")}
                    title="Generate the main still for this storyboard step"
                  >
                    {f.still_path ? "Replace hero still" : "Create hero still"}
                  </button>
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={!!busy}
                    onClick={() => generateVisual(f.id, "preview")}
                    title="One short video from this beat only — skips the keyframe pipeline"
                  >
                    Quick preview video
                  </button>
                </div>
                {mediaUrl(f.still_path) && (
                  <div className="still-edit">
                    <input
                      className="still-edit-input"
                      placeholder="Tweak the still… e.g. make the dress red"
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
                      title="Change the existing hero still using this instruction"
                    >
                      Apply still edit
                    </button>
                  </div>
                )}
                <div className="media-slot">
                  {(mediaUrl(f.still_path) ||
                    (visualBusy?.frameId === f.id &&
                      (visualBusy.kind === "still" || visualBusy.kind === "edit"))) && (
                    <div className="media-item">
                      {mediaUrl(f.still_path) ? (
                        <>
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
                        </>
                      ) : (
                        <div className="thumb-placeholder" aria-hidden="true" />
                      )}
                      {visualBusy?.frameId === f.id &&
                        (visualBusy.kind === "still" || visualBusy.kind === "edit") && (
                          <div className="thumb-overlay" aria-busy="true">
                            <span className="spinner" />
                            <span className="tiny">
                              {visualBusy.kind === "edit"
                                ? "Editing hero still…"
                                : "Creating hero still…"}
                            </span>
                          </div>
                        )}
                    </div>
                  )}
                  {(mediaUrl(f.preview_path) ||
                    (visualBusy?.frameId === f.id &&
                      (visualBusy.kind === "preview" ||
                        visualBusy.kind === "between" ||
                        visualBusy.kind === "step_clips"))) && (
                    <div className="media-item">
                      {mediaUrl(f.preview_path) ? (
                        <>
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
                        </>
                      ) : (
                        <div className="thumb-placeholder" aria-hidden="true" />
                      )}
                      {visualBusy?.frameId === f.id &&
                        (visualBusy.kind === "preview" ||
                          visualBusy.kind === "between" ||
                          visualBusy.kind === "step_clips") && (
                          <div className="thumb-overlay" aria-busy="true">
                            <span className="spinner" />
                            <span className="tiny">
                              {visualBusy.kind === "between"
                                ? "Bridging to next beat…"
                                : visualBusy.kind === "step_clips"
                                  ? "Animating this beat…"
                                  : "Creating quick preview…"}
                            </span>
                          </div>
                        )}
                    </div>
                  )}
                </div>
              </section>

              <h3 className="kf-series-heading">Keyframe series</h3>
              <div className="kf-phases">
                {draftKfs.map((kf, ki) => {
                  const path = kf.path;
                  const nice = keyframeRoleLabel(kf.role, ki, draftKfs.length);
                  const busyPhase =
                    visualBusy?.frameId === f.id &&
                    (visualBusy.kind === "keyframes" ||
                      visualBusy.kind === `keyframe_${ki}` ||
                      visualBusy.kind === `keyframe_${kf.role}`);
                  return (
                    <section key={`draft-kf-${ki}`} className="kf-phase">
                      <header>
                        <strong>
                          {nice} · t={kf.t_sec}s
                        </strong>
                        {mediaUrl(path) && (
                          <button
                            type="button"
                            className="linkish"
                            onClick={() =>
                              setLightbox({
                                frameId: f.id,
                                kind: `keyframe_${ki}`,
                                src: `${mediaUrl(path)}?t=${encodeURIComponent(path)}`,
                                label: `Frame ${f.position + 1} ${nice}`,
                              })
                            }
                          >
                            Enlarge
                          </button>
                        )}
                      </header>
                      <div className="kf-phase-body">
                        <div className="kf-phase-media">
                          {mediaUrl(path) ? (
                            <img
                              className="thumb keyframe-thumb"
                              src={`${mediaUrl(path)}?t=${encodeURIComponent(path)}`}
                              alt={`${nice} keyframe`}
                            />
                          ) : (
                            <div
                              className={`thumb-placeholder keyframe-thumb${
                                busyPhase ? " is-busy" : ""
                              }`}
                            >
                              {busyPhase && <span className="spinner" />}
                            </div>
                          )}
                          {busyPhase && mediaUrl(path) && (
                            <div className="thumb-overlay" aria-busy="true">
                              <span className="spinner" />
                            </div>
                          )}
                        </div>
                        <label className="kf-field">
                          <span>{nice} image prompt (sent to Comfy)</span>
                          <textarea
                            rows={5}
                            value={kf.image_prompt}
                            disabled={!!busy}
                            onChange={(e) =>
                              setEditorDraft((d) => {
                                const next = [...(d.keyframes || [])];
                                next[ki] = { ...next[ki], image_prompt: e.target.value };
                                return { ...d, keyframes: next };
                              })
                            }
                          />
                        </label>
                      </div>
                      <div className="row">
                        <button
                          type="button"
                          disabled={!!busy}
                          onClick={() => renderOneKeyframe(ki)}
                        >
                          Generate {nice.toLowerCase()} image
                        </button>
                      </div>
                      <div className="still-edit">
                        <input
                          className="still-edit-input"
                          placeholder={`Tweak ${nice.toLowerCase()}… e.g. warmer light`}
                          value={kfEditDrafts[ki] || ""}
                          disabled={!!busy}
                          onChange={(e) =>
                            setKfEditDrafts((prev) => ({
                              ...prev,
                              [ki]: e.target.value,
                            }))
                          }
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              editOneKeyframe(ki);
                            }
                          }}
                        />
                        <button
                          type="button"
                          disabled={!!busy || !(kfEditDrafts[ki] || "").trim()}
                          onClick={() => editOneKeyframe(ki)}
                        >
                          Apply {nice.toLowerCase()} edit
                        </button>
                      </div>
                    </section>
                  );
                })}
                {!draftKfs.length && (
                  <p className="muted">
                    No keyframe slots yet — click “Rebuild LLM keyframe prompts”.
                  </p>
                )}
              </div>

              <section className="frame-stage kf-editor-section">
                <div className="frame-stage-head">
                  <strong>Motion</strong>
                  <span className="tiny muted">FLF2V animate consecutive keyframes into a preview</span>
                </div>
                <div className="frame-stage-actions">
                  <button
                    type="button"
                    disabled={!!busy || !keyframesReady(f)}
                    onClick={() =>
                      run(`step clips ${f.id}`, async () => {
                        setVisualBusy({ frameId: f.id, kind: "step_clips" });
                        try {
                          await api(`/projects/${id}/storyboard/frames/${f.id}/step-clips`, {
                            method: "POST",
                            body: JSON.stringify({ num_frames: 33 }),
                          });
                        } finally {
                          setVisualBusy(null);
                        }
                      })
                    }
                    title={
                      keyframesReady(f)
                        ? "FLF2V between each consecutive keyframe (locks start and end), then combine into the preview"
                        : "Needs a complete keyframe series first"
                    }
                  >
                    Animate this beat
                  </button>
                  {(() => {
                    const frames = [...(project.frames || [])].sort(
                      (a, b) => a.position - b.position
                    );
                    const next = frames.find((x) => x.position === f.position + 1);
                    const thisLast =
                      frameKeyframes(f).slice(-1)[0]?.path || f.still_path;
                    const nextFirst =
                      frameKeyframes(next || {})[0]?.path || next?.still_path;
                    const canBetween = !!(thisLast && nextFirst);
                    return (
                      <button
                        type="button"
                        className="btn-secondary"
                        disabled={!!busy || !canBetween}
                        onClick={() =>
                          run(`between stills ${f.id}`, async () => {
                            setVisualBusy({ frameId: f.id, kind: "between" });
                            try {
                              await api(
                                `/projects/${id}/storyboard/frames/${f.id}/between-stills`,
                                {
                                  method: "POST",
                                  body: JSON.stringify({ num_frames: 33 }),
                                }
                              );
                            } finally {
                              setVisualBusy(null);
                            }
                          })
                        }
                        title={
                          canBetween
                            ? "FLF2V bridge from this beat’s end into the next beat’s start"
                            : "Needs this beat’s end image and the next beat’s start image"
                        }
                      >
                        Bridge into next beat
                      </button>
                    );
                  })()}
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={!!busy}
                    onClick={() =>
                      run(`keyframes ${f.id}`, async () => {
                        setVisualBusy({ frameId: f.id, kind: "keyframes" });
                        try {
                          await patchEditorFields();
                          await api(`/projects/${id}/storyboard/frames/${f.id}/keyframes`, {
                            method: "POST",
                            body: JSON.stringify({ skip_existing: true }),
                          });
                        } finally {
                          setVisualBusy(null);
                        }
                      })
                    }
                    title="Create any missing images in this beat’s keyframe series"
                  >
                    Create missing keyframe images
                  </button>
                </div>
              </section>
            </div>
          </div>
        );
      })()}
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
