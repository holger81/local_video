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

/** Secure-context-safe short id (crypto.randomUUID is missing on plain HTTP LAN). */
function newId(prefix = "") {
  try {
    if (globalThis.crypto?.randomUUID) {
      return `${prefix}${crypto.randomUUID().replace(/-/g, "").slice(0, 10)}`;
    }
  } catch {
    /* fall through */
  }
  return `${prefix}${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
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
          <Link to="/settings">Settings</Link>
        </nav>
      </header>
      <main>{children}</main>
    </div>
  );
}

function formatCtx(n) {
  if (!n || n <= 0) return "—";
  if (n >= 1024) return `${Math.round(n / 1024)}k`;
  return String(n);
}

function SettingsPage() {
  const [settings, setSettings] = useState(null);
  const [models, setModels] = useState([]);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [nCtx, setNCtx] = useState("");
  const [maxTokens, setMaxTokens] = useState("2048");
  const [videoBackend, setVideoBackend] = useState("wan");
  const [videoBackends, setVideoBackends] = useState([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");

  const load = useCallback(async () => {
    setErr("");
    const s = await api("/settings");
    setSettings(s);
    setBaseUrl(s.llama_base_url || "");
    setModel(s.llama_model || "");
    setApiKey("");
    setNCtx(s.llama_n_ctx ? String(s.llama_n_ctx) : "");
    setMaxTokens(String(s.llama_max_tokens || 2048));
    setVideoBackend(s.default_video_backend || "wan");
    try {
      const vb = await api("/video-backends");
      setVideoBackends(vb.backends || []);
    } catch {
      setVideoBackends([]);
    }
    try {
      const m = await api("/llm/models");
      setModels(m.models || []);
    } catch (e) {
      setModels([]);
      setErr(String(e.message || e));
    }
  }, []);

  useEffect(() => {
    load().catch((e) => setErr(String(e.message || e)));
  }, [load]);

  const onModelChange = (id) => {
    setModel(id);
    const match = models.find((m) => m.id === id);
    if (match?.n_ctx) setNCtx(String(match.n_ctx));
  };

  const save = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    setOk("");
    try {
      const body = {
        llama_base_url: baseUrl.trim(),
        llama_model: model.trim(),
        llama_max_tokens: Number(maxTokens) || 2048,
        default_video_backend: videoBackend,
      };
      if (apiKey.trim()) body.llama_api_key = apiKey.trim();
      if (nCtx.trim()) body.llama_n_ctx = Number(nCtx);
      const s = await api("/settings", { method: "PUT", body: JSON.stringify(body) });
      setSettings(s);
      setOk("Saved. New LLM calls use this model and context budget.");
      const m = await api("/llm/models");
      setModels(m.models || []);
    } catch (ex) {
      setErr(String(ex.message || ex));
    } finally {
      setBusy(false);
    }
  };

  const refreshModels = async () => {
    setBusy(true);
    setErr("");
    try {
      if (baseUrl.trim() !== settings?.llama_base_url) {
        await api("/settings", {
          method: "PUT",
          body: JSON.stringify({ llama_base_url: baseUrl.trim() }),
        });
      }
      const m = await api("/llm/models");
      setModels(m.models || []);
      setOk(`Loaded ${m.models?.length || 0} models from server.`);
    } catch (ex) {
      setErr(String(ex.message || ex));
    } finally {
      setBusy(false);
    }
  };

  const selectedMeta = models.find((m) => m.id === model);

  return (
    <Shell>
      <section className="settings-page">
        <h1>Settings</h1>
        <p className="muted">
          Choose which llama.cpp model story / storyboard prompts use. Context size
          caps prompt length and completion tokens so large stories do not overflow
          the window.
        </p>
        {err && <p className="error">{err}</p>}
        {ok && <p className="ok">{ok}</p>}
        <form className="settings-form" onSubmit={save}>
          <label>
            LLM base URL
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://host:9292/v1"
              required
            />
          </label>
          <div className="row">
            <button type="button" className="ghost" disabled={busy} onClick={refreshModels}>
              Refresh models
            </button>
            <span className="tiny muted">
              {models.length ? `${models.length} available` : "No models loaded yet"}
            </span>
          </div>
          <label>
            Model
            <select value={model} onChange={(e) => onModelChange(e.target.value)} required>
              {!models.some((m) => m.id === model) && model && (
                <option value={model}>{model} (current)</option>
              )}
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.status === "loaded" ? "● " : "○ "}
                  {m.id}
                  {m.n_ctx ? ` · ctx ${formatCtx(m.n_ctx)}` : ""}
                </option>
              ))}
            </select>
          </label>
          {selectedMeta && (
            <p className="muted tiny settings-model-meta">
              Status: {selectedMeta.status}
              {selectedMeta.n_ctx_loaded
                ? ` · loaded ctx ${formatCtx(selectedMeta.n_ctx_loaded)}`
                : ""}
              {selectedMeta.n_ctx_configured
                ? ` · configured ${formatCtx(selectedMeta.n_ctx_configured)}`
                : ""}
              {selectedMeta.n_ctx_train
                ? ` · train ${formatCtx(selectedMeta.n_ctx_train)}`
                : ""}
              {selectedMeta.input_modalities?.length
                ? ` · ${selectedMeta.input_modalities.join("+")}`
                : ""}
            </p>
          )}
          <div className="settings-grid">
            <label>
              Context budget (tokens)
              <input
                type="number"
                min="0"
                step="256"
                value={nCtx}
                onChange={(e) => setNCtx(e.target.value)}
                placeholder="auto from model"
              />
            </label>
            <label>
              Max completion tokens
              <input
                type="number"
                min="64"
                max="128000"
                step="64"
                value={maxTokens}
                onChange={(e) => setMaxTokens(e.target.value)}
                required
              />
            </label>
          </div>
          <p className="muted tiny">
            Completion is capped to about ¼ of the context budget. Long story inputs
            are truncated to fit the remaining prompt window.
          </p>
          <label>
            API key {settings?.llama_api_key_set ? "(set — leave blank to keep)" : "(optional)"}
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="not-needed"
              autoComplete="off"
            />
          </label>
          <label>
            Default video backend
            <select value={videoBackend} onChange={(e) => setVideoBackend(e.target.value)}>
              <option value="wan">Wan 2.2</option>
              <option value="ltx">LTX</option>
            </select>
          </label>
          {videoBackend === "ltx" &&
            videoBackends.some((b) => b.id === "ltx" && !b.flf2v_ready) && (
              <p className="error">
                LTX FLF workflows are not installed yet — export API graphs into
                comfyui_workflows/api/ before rendering with LTX.
              </p>
            )}
          <div className="row">
            <button type="submit" disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
        {models.length > 0 && (
          <div className="settings-model-list">
            <h3>Available models</h3>
            <ul>
              {models.map((m) => (
                <li key={m.id} className={m.id === model ? "selected" : ""}>
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => onModelChange(m.id)}
                  >
                    {m.id}
                  </button>
                  <span className="tiny muted">
                    {m.status}
                    {m.n_ctx ? ` · ctx ${formatCtx(m.n_ctx)}` : " · ctx unknown"}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </Shell>
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
  const [characterEditorId, setCharacterEditorId] = useState(null);
  const [charDraft, setCharDraft] = useState(null);
  const [charEditInstr, setCharEditInstr] = useState("");
  const [outfitEditInstr, setOutfitEditInstr] = useState({});
  const [err, setErr] = useState("");
  const [job, setJob] = useState(null);
  const [movies, setMovies] = useState([]);
  const [videoBackends, setVideoBackends] = useState([]);
  const [movieForm, setMovieForm] = useState({
    target_length_sec: 20,
    chunk_frames: 33,
    overlap_frames: 12,
    format: "mp4",
    aspect: "16:9",
    seed: 42,
    video_backend: "wan",
  });
  const [shotBackends, setShotBackends] = useState({});

  const loadAssets = useCallback(async () => {
    const a = await api(`/projects/${id}/assets`);
    setMovies((a.movies || []).filter((m) => m.movie_path && m.status === "completed"));
    return a;
  }, [id]);

  const load = useCallback(async () => {
    const p = await api(`/projects/${id}`);
    setProject(p);
    setStoryEdit(p.story || "");
    setMovieForm((prev) => ({
      ...prev,
      video_backend: p.video_backend || prev.video_backend || "wan",
    }));
    try {
      const vb = await api("/video-backends");
      setVideoBackends(vb.backends || []);
    } catch {
      setVideoBackends([]);
    }
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
      cast: Array.isArray(f.cast)
        ? f.cast.map((x) => ({
            character_id: x.character_id,
            outfit_id: x.outfit_id || null,
          }))
        : [],
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

  useEffect(() => {
    if (!characterEditorId || !project) return;
    const c = (project.characters || []).find((x) => x.id === characterEditorId);
    if (!c) {
      setCharacterEditorId(null);
      setCharDraft(null);
      return;
    }
    setCharDraft({
      name: c.name || "",
      description: c.description || "",
      appearance_prompt: c.appearance_prompt || "",
      aliases: Array.isArray(c.aliases) ? c.aliases.join(", ") : "",
      approved: !!c.approved,
      reference_image_path: c.reference_image_path || null,
      auto_detected: !!c.auto_detected,
      intro_frame_id: c.intro_frame_id ?? null,
      outfits: Array.isArray(c.outfits)
        ? c.outfits.map((o) => ({
            id: o.id,
            name: o.name || "Outfit",
            prompt: o.prompt || "",
            reference_image_path: o.reference_image_path || null,
            is_default: !!o.is_default,
          }))
        : [],
    });
  }, [characterEditorId, project]);

  useEffect(() => {
    setCharEditInstr("");
    setOutfitEditInstr({});
  }, [characterEditorId]);

  useEffect(() => {
    if (!characterEditorId) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape" && !lightbox) {
        setCharacterEditorId(null);
        setCharDraft(null);
        setCharEditInstr("");
        setOutfitEditInstr({});
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [characterEditorId, lightbox]);

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
        cast: (editorDraft.cast || []).map((x) => ({
          character_id: x.character_id,
          outfit_id: x.outfit_id || null,
        })),
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
        <label className="project-backend">
          Project video backend
          <select
            value={project.video_backend || "wan"}
            disabled={!!busy}
            onChange={(e) =>
              run("update project backend", async () => {
                const p = await api(`/projects/${id}`, {
                  method: "PATCH",
                  body: JSON.stringify({ video_backend: e.target.value }),
                });
                setProject(p);
                setMovieForm((prev) => ({
                  ...prev,
                  video_backend: p.video_backend || "wan",
                }));
              })
            }
          >
            <option value="wan">Wan 2.2</option>
            <option value="ltx">LTX</option>
          </select>
        </label>
        {(project.video_backend || "wan") === "ltx" &&
          videoBackends.some((b) => b.id === "ltx" && !b.flf2v_ready) && (
            <p className="error">
              LTX FLF workflows are not installed — import API graphs before rendering.
            </p>
          )}
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
        <h2>2. Characters</h2>
        <p className="muted">
          Cast ground truth for stills and keyframes. Auto-detected when the story
          introduces someone; edit appearance and generate a reference portrait.
        </p>
        <div className="row">
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("detect characters", () =>
                api(`/projects/${id}/characters/detect`, {
                  method: "POST",
                  body: JSON.stringify({ replace_auto: false }),
                })
              )
            }
          >
            Detect from story
          </button>
          <button
            type="button"
            disabled={!!busy}
            onClick={async () => {
              setErr("");
              try {
                const c = await api(`/projects/${id}/characters`, {
                  method: "POST",
                  body: JSON.stringify({
                    name: "New character",
                    description: "",
                    appearance_prompt: "",
                  }),
                });
                await load();
                setCharacterEditorId(c.id);
              } catch (ex) {
                setErr(String(ex.message || ex));
              }
            }}
          >
            Add character
          </button>
        </div>
        <div className="characters-grid">
          {(project.characters || []).map((c) => {
            const src = mediaUrl(c.reference_image_path);
            return (
              <button
                key={c.id}
                type="button"
                className="character-card"
                onClick={() => setCharacterEditorId(c.id)}
              >
                {src ? (
                  <img src={`${src}?t=${encodeURIComponent(c.reference_image_path)}`} alt="" />
                ) : (
                  <div className="character-card-placeholder">No ref</div>
                )}
                <div className="character-card-meta">
                  <strong>{c.name || "Unnamed"}</strong>
                  <span className="tiny muted">
                    {c.approved ? "approved" : c.auto_detected ? "auto" : "draft"}
                    {c.intro_frame_id ? ` · intro #${c.intro_frame_id}` : ""}
                  </span>
                </div>
              </button>
            );
          })}
          {!(project.characters || []).length && (
            <p className="muted tiny">No characters yet — generate/approve a story or detect.</p>
          )}
        </div>
      </section>

      <section className="card-like">
        <h2>3. Storyboard</h2>
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
        <h2>4. Batch keyframes &amp; motion</h2>
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
        <h2>5. Movie wizard</h2>
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
          <label>
            Movie video backend
            <select
              value={movieForm.video_backend || "wan"}
              onChange={(e) =>
                setMovieForm({ ...movieForm, video_backend: e.target.value })
              }
            >
              <option value="wan">Wan 2.2</option>
              <option value="ltx">LTX</option>
            </select>
          </label>
        </div>
        {(movieForm.video_backend || "wan") === "ltx" &&
          videoBackends.some((b) => b.id === "ltx" && !b.flf2v_ready) && (
            <p className="error">
              LTX is selected but FLF workflows are missing — movie render will fail until
              you export ltx_flf2v into comfyui_workflows/api/.
            </p>
          )}
        {(project.frames || []).length > 0 && (
          <div className="shot-backends">
            <h3>Per-shot backend</h3>
            <p className="muted tiny">
              Default uses the movie backend. Prefer a new shot when switching Wan ↔ LTX.
            </p>
            <ul className="list">
              {(project.frames || [])
                .filter((f) => f.is_new_shot || f.position === 0)
                .map((f) => (
                  <li key={f.id} className="row">
                    <span>
                      Shot from step #{f.position + 1}
                      {f.is_new_shot ? "" : " (first)"}
                    </span>
                    <select
                      value={shotBackends[String(f.id)] || ""}
                      onChange={(e) => {
                        const v = e.target.value;
                        setShotBackends((prev) => {
                          const next = { ...prev };
                          if (!v) delete next[String(f.id)];
                          else next[String(f.id)] = v;
                          return next;
                        });
                      }}
                    >
                      <option value="">Default ({movieForm.video_backend || "wan"})</option>
                      <option value="wan">Wan</option>
                      <option value="ltx">LTX</option>
                    </select>
                  </li>
                ))}
            </ul>
          </div>
        )}
        <div className="row">
          <button
            type="button"
            disabled={!!busy}
            onClick={() =>
              run("start movie", async () => {
                const body = { ...movieForm };
                if (Object.keys(shotBackends).length) {
                  body.shot_backends = shotBackends;
                }
                const j = await api(`/projects/${id}/movies`, {
                  method: "POST",
                  body: JSON.stringify(body),
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
                    {s.video_backend ? ` · ${s.video_backend}` : job.video_backend ? ` · ${job.video_backend}` : ""}
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
                const deleteMovie = async () => {
                  if (
                    !window.confirm(
                      `Delete movie for job #${m.job_id}? This removes the file and job record.`
                    )
                  ) {
                    return;
                  }
                  setErr("");
                  try {
                    await api(`/jobs/${m.job_id}`, { method: "DELETE" });
                    if (job?.id === m.job_id) setJob(null);
                    await loadAssets();
                  } catch (ex) {
                    setErr(String(ex.message || ex));
                  }
                };
                if (!src) {
                  return (
                    <div key={m.job_id} className="movie-card">
                      <p className="muted tiny">
                        Job #{m.job_id} · path not served ({m.movie_path})
                      </p>
                      <div className="row movie-card-actions">
                        <button type="button" className="ghost danger" onClick={deleteMovie}>
                          Delete
                        </button>
                      </div>
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
                      <div className="row movie-card-links">
                        <a
                          className="linkish"
                          href={src}
                          download={`project-${id}-job-${m.job_id}.mp4`}
                        >
                          Download
                        </a>
                        <button type="button" className="ghost danger" onClick={deleteMovie}>
                          Delete
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </section>
      {characterEditorId && charDraft && (() => {
        const c = (project.characters || []).find((x) => x.id === characterEditorId);
        if (!c) return null;
        const refSrc = mediaUrl(charDraft.reference_image_path);
        const saveChar = async () => {
          setErr("");
          try {
            await api(`/projects/${id}/characters/${c.id}`, {
              method: "PATCH",
              body: JSON.stringify({
                name: charDraft.name,
                description: charDraft.description,
                appearance_prompt: charDraft.appearance_prompt,
                aliases: charDraft.aliases
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
                approved: !!charDraft.approved,
                outfits: (charDraft.outfits || []).map((o) => ({
                  id: o.id,
                  name: o.name,
                  prompt: o.prompt,
                  reference_image_path: o.reference_image_path || null,
                  is_default: !!o.is_default,
                })),
              }),
            });
            await load();
          } catch (ex) {
            setErr(String(ex.message || ex));
          }
        };
        return (
          <div
            className="kf-editor"
            role="dialog"
            aria-modal="true"
            aria-label={`Character editor ${charDraft.name || c.id}`}
            onClick={() => {
              if (!busy) {
                setCharacterEditorId(null);
                setCharDraft(null);
                setCharEditInstr("");
                setOutfitEditInstr({});
              }
            }}
          >
            <div className="kf-editor-inner" onClick={(e) => e.stopPropagation()}>
              <header className="kf-editor-head">
                <div>
                  <h2>{charDraft.name || "Character"}</h2>
                  <p className="muted tiny">
                    Ground-truth look for storyboard stills and keyframes.
                    {charDraft.auto_detected ? " Auto-detected from story." : ""}
                  </p>
                </div>
                <button
                  type="button"
                  className="ghost"
                  disabled={!!busy}
                  onClick={() => {
                    setCharacterEditorId(null);
                    setCharDraft(null);
                    setCharEditInstr("");
                    setOutfitEditInstr({});
                  }}
                >
                  Close
                </button>
              </header>
              <div className="kf-editor-fields">
                <label>
                  Name
                  <input
                    value={charDraft.name}
                    onChange={(e) =>
                      setCharDraft((d) => ({ ...d, name: e.target.value }))
                    }
                  />
                </label>
                <label>
                  Aliases (comma-separated)
                  <input
                    value={charDraft.aliases}
                    onChange={(e) =>
                      setCharDraft((d) => ({ ...d, aliases: e.target.value }))
                    }
                    placeholder="nicknames used in the story…"
                  />
                </label>
                <label>
                  Role / description
                  <textarea
                    rows={3}
                    value={charDraft.description}
                    onChange={(e) =>
                      setCharDraft((d) => ({ ...d, description: e.target.value }))
                    }
                  />
                </label>
                <label>
                  Appearance prompt (ground truth)
                  <textarea
                    rows={4}
                    value={charDraft.appearance_prompt}
                    onChange={(e) =>
                      setCharDraft((d) => ({
                        ...d,
                        appearance_prompt: e.target.value,
                      }))
                    }
                    placeholder="Age, face, hair, body — keep wardrobe in Outfits below"
                  />
                </label>
                <div className="character-outfits">
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <h3 style={{ margin: 0 }}>Outfits</h3>
                    <button
                      type="button"
                      className="ghost"
                      disabled={!!busy}
                      onClick={() =>
                        setCharDraft((d) => ({
                          ...d,
                          outfits: [
                            ...(d.outfits || []),
                            {
                              id: newId(),
                              name: "New outfit",
                              prompt: "",
                              reference_image_path: null,
                              is_default: !(d.outfits || []).length,
                            },
                          ],
                        }))
                      }
                    >
                      Add outfit
                    </button>
                  </div>
                  <p className="muted tiny">
                    Design wardrobe looks separately from face/body. Pick an outfit per
                    storyboard beat.
                  </p>
                  {(charDraft.outfits || []).map((o, oi) => {
                    const oRef = mediaUrl(o.reference_image_path);
                    return (
                      <div key={o.id || oi} className="outfit-card">
                        <div className="row" style={{ gap: "0.5rem", flexWrap: "wrap" }}>
                          <input
                            value={o.name}
                            disabled={!!busy}
                            onChange={(e) =>
                              setCharDraft((d) => {
                                const outfits = [...(d.outfits || [])];
                                outfits[oi] = { ...outfits[oi], name: e.target.value };
                                return { ...d, outfits };
                              })
                            }
                            placeholder="Outfit name"
                            style={{ flex: "1 1 8rem" }}
                          />
                          <label className="row" style={{ alignItems: "center", gap: "0.35rem" }}>
                            <input
                              type="radio"
                              name={`default-outfit-${c.id}`}
                              checked={!!o.is_default}
                              disabled={!!busy}
                              onChange={() =>
                                setCharDraft((d) => ({
                                  ...d,
                                  outfits: (d.outfits || []).map((x, i) => ({
                                    ...x,
                                    is_default: i === oi,
                                  })),
                                }))
                              }
                            />
                            Default
                          </label>
                          <button
                            type="button"
                            className="ghost danger"
                            disabled={!!busy}
                            onClick={() =>
                              setCharDraft((d) => {
                                const outfits = (d.outfits || []).filter((_, i) => i !== oi);
                                if (outfits.length && !outfits.some((x) => x.is_default)) {
                                  outfits[0] = { ...outfits[0], is_default: true };
                                }
                                return { ...d, outfits };
                              })
                            }
                          >
                            Remove
                          </button>
                        </div>
                        <textarea
                          rows={2}
                          value={o.prompt}
                          disabled={!!busy}
                          onChange={(e) =>
                            setCharDraft((d) => {
                              const outfits = [...(d.outfits || [])];
                              outfits[oi] = { ...outfits[oi], prompt: e.target.value };
                              return { ...d, outfits };
                            })
                          }
                          placeholder="Clothing description: colors, garments, accessories…"
                        />
                        {oRef && (
                          <div className="media-item">
                            <img
                              src={`${oRef}?t=${encodeURIComponent(o.reference_image_path)}`}
                              alt=""
                            />
                          </div>
                        )}
                        <button
                          type="button"
                          disabled={!!busy || !o.prompt.trim() || visualBusy === `outfit-${o.id}`}
                          onClick={async () => {
                            setVisualBusy(`outfit-${o.id}`);
                            setErr("");
                            try {
                              await saveChar();
                              await api(
                                `/projects/${id}/characters/${c.id}/outfits/${o.id}/reference`,
                                { method: "POST", body: "{}" }
                              );
                              await load();
                            } catch (ex) {
                              setErr(String(ex.message || ex));
                            } finally {
                              setVisualBusy(null);
                            }
                          }}
                        >
                          {oRef ? "Regenerate outfit look" : "Generate outfit look"}
                        </button>
                        {oRef && (
                          <>
                            <label>
                              Edit outfit instruction
                              <input
                                value={outfitEditInstr[o.id] || ""}
                                disabled={!!busy}
                                onChange={(e) =>
                                  setOutfitEditInstr((prev) => ({
                                    ...prev,
                                    [o.id]: e.target.value,
                                  }))
                                }
                                placeholder="e.g. add a scarf, darker jacket…"
                              />
                            </label>
                            <button
                              type="button"
                              disabled={
                                !!busy ||
                                !(outfitEditInstr[o.id] || "").trim() ||
                                visualBusy === `outfit-${o.id}`
                              }
                              onClick={async () => {
                                const instruction = (outfitEditInstr[o.id] || "").trim();
                                if (!instruction) return;
                                setVisualBusy(`outfit-${o.id}`);
                                setErr("");
                                try {
                                  await saveChar();
                                  await api(
                                    `/projects/${id}/characters/${c.id}/outfits/${o.id}/reference`,
                                    {
                                      method: "POST",
                                      body: JSON.stringify({ instruction }),
                                    }
                                  );
                                  setOutfitEditInstr((prev) => {
                                    const next = { ...prev };
                                    delete next[o.id];
                                    return next;
                                  });
                                  await load();
                                } catch (ex) {
                                  setErr(String(ex.message || ex));
                                } finally {
                                  setVisualBusy(null);
                                }
                              }}
                            >
                              Apply edit to outfit
                            </button>
                          </>
                        )}
                      </div>
                    );
                  })}
                  {!(charDraft.outfits || []).length && (
                    <p className="muted tiny">No outfits yet — add one to design clothes.</p>
                  )}
                </div>
                <label className="row" style={{ alignItems: "center", gap: "0.5rem" }}>
                  <input
                    type="checkbox"
                    checked={!!charDraft.approved}
                    onChange={(e) =>
                      setCharDraft((d) => ({ ...d, approved: e.target.checked }))
                    }
                  />
                  Approved look
                </label>
                <div className="row">
                  <button type="button" disabled={!!busy} onClick={saveChar}>
                    Save
                  </button>
                  <button
                    type="button"
                    className="ghost danger"
                    disabled={!!busy}
                    onClick={async () => {
                      if (!window.confirm(`Delete character “${charDraft.name}”?`)) return;
                      try {
                        await api(`/projects/${id}/characters/${c.id}`, {
                          method: "DELETE",
                        });
                        setCharacterEditorId(null);
                        setCharDraft(null);
                        await load();
                      } catch (ex) {
                        setErr(String(ex.message || ex));
                      }
                    }}
                  >
                    Delete
                  </button>
                </div>
              </div>
              <div className="character-ref-block">
                <h3>Reference still</h3>
                {refSrc ? (
                  <div className="media-item">
                    <img
                      src={`${refSrc}?t=${encodeURIComponent(charDraft.reference_image_path)}`}
                      alt=""
                      onClick={() =>
                        setLightbox({
                          frameId: null,
                          kind: "character",
                          src: `${refSrc}?t=${encodeURIComponent(charDraft.reference_image_path)}`,
                          label: charDraft.name,
                        })
                      }
                    />
                  </div>
                ) : (
                  <p className="muted tiny">No reference image yet.</p>
                )}
                <div className="row">
                  <button
                    type="button"
                    disabled={!!busy || visualBusy === `char-${c.id}`}
                    onClick={async () => {
                      setVisualBusy(`char-${c.id}`);
                      setErr("");
                      try {
                        await saveChar();
                        await api(`/projects/${id}/characters/${c.id}/reference`, {
                          method: "POST",
                          body: JSON.stringify({}),
                        });
                        await load();
                      } catch (ex) {
                        setErr(String(ex.message || ex));
                      } finally {
                        setVisualBusy(null);
                      }
                    }}
                  >
                    {refSrc ? "Regenerate reference" : "Generate reference"}
                  </button>
                  {refSrc && (
                    <button
                      type="button"
                      className="ghost danger"
                      disabled={!!busy}
                      onClick={async () => {
                        try {
                          await api(`/projects/${id}/characters/${c.id}/reference`, {
                            method: "DELETE",
                          });
                          await load();
                        } catch (ex) {
                          setErr(String(ex.message || ex));
                        }
                      }}
                    >
                      Clear reference
                    </button>
                  )}
                </div>
                <label>
                  Edit instruction
                  <input
                    value={charEditInstr}
                    onChange={(e) => setCharEditInstr(e.target.value)}
                    placeholder="e.g. shorter hair, green jacket…"
                  />
                </label>
                <button
                  type="button"
                  disabled={!!busy || !charEditInstr.trim() || !refSrc}
                  onClick={async () => {
                    setVisualBusy(`char-${c.id}`);
                    setErr("");
                    try {
                      await api(`/projects/${id}/characters/${c.id}/reference`, {
                        method: "POST",
                        body: JSON.stringify({ instruction: charEditInstr.trim() }),
                      });
                      setCharEditInstr("");
                      await load();
                    } catch (ex) {
                      setErr(String(ex.message || ex));
                    } finally {
                      setVisualBusy(null);
                    }
                  }}
                >
                  Apply edit to reference
                </button>
              </div>
            </div>
          </div>
        );
      })()}
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
                    ≤2s gaps. {f.is_new_shot ? "New shot = own series." : "Continue = shares prior end as this start (exact image)."}
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

                <div className="scene-cast">
                  <h3>Cast &amp; wardrobe</h3>
                  <p className="muted tiny">
                    Select who appears in this beat and which outfit. Empty = full cast
                    defaults.
                  </p>
                  {(project.characters || []).length === 0 ? (
                    <p className="muted tiny">No characters yet — add them in the cast board.</p>
                  ) : (
                    (project.characters || []).map((ch) => {
                      const entry = (editorDraft.cast || []).find(
                        (x) => x.character_id === ch.id
                      );
                      const on = !!entry;
                      const outfits = Array.isArray(ch.outfits) ? ch.outfits : [];
                      return (
                        <div key={ch.id} className="scene-cast-row">
                          <label className="row" style={{ alignItems: "center", gap: "0.4rem" }}>
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={!!busy}
                              onChange={(e) => {
                                const checked = e.target.checked;
                                setEditorDraft((d) => {
                                  const cast = [...(d.cast || [])].filter(
                                    (x) => x.character_id !== ch.id
                                  );
                                  if (checked) {
                                    const def =
                                      outfits.find((o) => o.is_default) || outfits[0];
                                    cast.push({
                                      character_id: ch.id,
                                      outfit_id: def?.id || null,
                                    });
                                  }
                                  return { ...d, cast };
                                });
                              }}
                            />
                            <strong>{ch.name}</strong>
                          </label>
                          {on && (
                            <select
                              disabled={!!busy || !outfits.length}
                              value={entry?.outfit_id || ""}
                              onChange={(e) => {
                                const oid = e.target.value || null;
                                setEditorDraft((d) => ({
                                  ...d,
                                  cast: (d.cast || []).map((x) =>
                                    x.character_id === ch.id
                                      ? { ...x, outfit_id: oid }
                                      : x
                                  ),
                                }));
                              }}
                            >
                              {!outfits.length && (
                                <option value="">Base appearance only</option>
                              )}
                              {outfits.map((o) => (
                                <option key={o.id} value={o.id}>
                                  {o.name}
                                  {o.is_default ? " (default)" : ""}
                                </option>
                              ))}
                            </select>
                          )}
                        </div>
                      );
                    })
                  )}
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
      <Route path="/settings" element={<SettingsPage />} />
    </Routes>
  );
}
