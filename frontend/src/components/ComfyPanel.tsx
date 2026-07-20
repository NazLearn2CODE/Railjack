import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import { fetchJSON, usePolling } from "../api";
import { useStore, type ModuleConfig } from "../store";

/**
 * Comfy Lab — local image/video generation panel. Polls service state + jobs,
 * drives the model downloader (catalog → /download), and runs text-to-image
 * generation (/generate). Two confidence modes: CONFIDENT fires /generate
 * directly; otherwise SEND TO TAWHAN types the request into the tmux pane for
 * human review (mirrors CockpitControls' /terminal/insert). Mirrors FfmpegPanel's
 * structure: hud cards, reveal stagger, Pip + progress-bar job rows.
 */

interface ComfyState {
  available: boolean;
  reason: string | null;
  up: boolean;
  vram_free_gb: number;
  vram_total_gb: number;
}

interface ModelComponent {
  folder: string;
  filename: string;
  url: string;
  size_gb: number;
  installed: boolean;
}
interface CatalogEntry {
  id: string;
  name: string;
  family: string;
  modality: "image" | "video";
  variant: string;
  vram_gb: number;
  proven: boolean;
  unrestricted: boolean;
  installed: boolean;
  fit: "ok" | "warn" | "no";
  size_gb: number;
  components: ModelComponent[];
}
interface Installed { image: string[]; video: string[] }

interface Job {
  id: string;
  kind: string;
  label: string;
  entry_id: string | null;
  status: string;
  progress: number;
  output_paths: string[] | null;
  error: string | null;
  logs: string[];
}

interface OutputFile { name: string; path: string; mtime: number }

const ASPECTS = [
  { v: "1920x1080", label: "1920×1080" },
  { v: "1080x1920", label: "1080×1920" },
  { v: "1024x1024", label: "1024×1024" },
];

const fileUrl = (p: string) => `/api/comfyui/outputs/file?path=${encodeURIComponent(p)}`;
const ls = (k: string) => localStorage.getItem(k);

function Pip({ status }: { status: string }) {
  const cls =
    status === "done" ? "pip pip--go" :
    status === "error" ? "pip pip--crit" :
    status === "running" ? "pip pip--signal" :
    status === "cancelled" ? "pip pip--hazard" : "pip";
  return <span className={cls} />;
}

export default function ComfyPanel({ module }: { module: ModuleConfig }) {
  const beginStarting = useStore((s) => s.beginStarting);

  // Persisted controls.
  const [model, setModel] = useState<string>(() => ls("comfy.model") ?? "");
  const [aspect, setAspect] = useState<string>(() => ls("comfy.aspect") ?? "1920x1080");
  const [confident, setConfident] = useState<boolean>(() => ls("comfy.confident") !== "off");

  // Prompt composer.
  const [prompt, setPrompt] = useState("");
  const [aiFill, setAiFill] = useState(false);
  const [subject, setSubject] = useState("");
  const [mood, setMood] = useState("");
  const [stylev, setStylev] = useState("");
  const [lighting, setLighting] = useState("");
  const [expanding, setExpanding] = useState(false);

  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [installed, setInstalled] = useState<Installed>({ image: [], video: [] });
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Default OFF every load: uncensored checkpoints stay hidden until toggled.
  const [showUnrestricted, setShowUnrestricted] = useState(false);

  const { data: state } = usePolling<ComfyState>("/api/comfyui/state", 5000);
  const { data: jobsData } = usePolling<{ jobs: Job[] }>("/api/comfyui/jobs", 1000);
  const { data: outData } = usePolling<{ files: OutputFile[] }>("/api/comfyui/outputs?limit=24", 10000);
  const jobs = jobsData?.jobs ?? [];
  const files = [...(outData?.files ?? [])].sort((a, b) => b.mtime - a.mtime);

  // Catalog + installed (re-fetched on mount and after an uninstall).
  const refresh = useCallback(() => {
    fetchJSON<{ entries: CatalogEntry[] }>("/api/comfyui/catalog")
      .then((r) => setCatalog(r.entries)).catch(() => setCatalog([]));
    fetchJSON<Installed>("/api/comfyui/installed")
      .then(setInstalled).catch(() => {});
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  // When the last live download finishes, re-fetch catalog/installed so the
  // Model Bay flips components to INSTALLED without a manual reload.
  const activeDownloads = jobs.filter(
    (j) => j.kind === "download" && (j.status === "queued" || j.status === "running"),
  ).length;
  const prevActive = useRef(0);
  useEffect(() => {
    if (prevActive.current > 0 && activeDownloads === 0) refresh();
    prevActive.current = activeDownloads;
  }, [activeDownloads, refresh]);

  // Keep the saved model valid once the installed list lands.
  useEffect(() => {
    if (installed.image.length && !installed.image.includes(model)) {
      const m = installed.image[0];
      setModel(m);
      localStorage.setItem("comfy.model", m);
    }
  }, [installed, model]);

  // Drop the local "starting" flag the moment the service reports up.
  useEffect(() => {
    if (state?.up && starting) setStarting(false);
  }, [state?.up, starting]);

  const up = !!state?.up;
  const pipCls = up ? "pip pip--go" : starting ? "pip pip--hazard" : "pip pip--crit";

  const start = async () => {
    setStarting(true);
    beginStarting(module.id, module.start_timeout_s ?? 90);
    try {
      await fetchJSON(`/api/modules/${module.id}/action`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: "start" }),
      });
    } catch {
      setStarting(false);
    }
  };
  const stop = async () => {
    try {
      await fetchJSON(`/api/modules/${module.id}/action`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ action: "stop" }),
      });
    } catch { /* non-fatal */ }
  };

  const download = async (entry_id: string) => {
    try {
      await fetchJSON<{ job_ids: string[] }>("/api/comfyui/download", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ entry_id }),
      });
    } catch { /* jobs card surfaces failures */ }
  };

  const cancelEntry = async (entry_id: string) => {
    try {
      await fetchJSON<{ cancelled: string[] }>("/api/comfyui/download/cancel", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ entry_id }),
      });
    } catch { /* jobs card surfaces failures */ }
  };

  const uninstall = async (entry_id: string) => {
    try {
      await fetchJSON<{ deleted: string[] }>("/api/comfyui/delete", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ entry_id }),
      });
      refresh();
    } catch { /* non-fatal */ }
  };

  const compose = () => {
    const parts = [
      subject,
      mood && `${mood} mood`,
      stylev && `${stylev} style`,
      lighting && `${lighting} lighting`,
      "highly detailed",
    ].filter(Boolean);
    setPrompt(parts.join(", "));
  };

  const expand = async () => {
    const idea = aiFill ? subject : prompt;
    if (!idea.trim()) return;
    setExpanding(true);
    try {
      const r = await fetchJSON<{ prompt: string }>("/api/comfyui/expand", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ idea, style: stylev || undefined, aspect }),
      });
      setPrompt(r.prompt);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setExpanding(false);
    }
  };

  const onAttachTxt = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const r = new FileReader();
    r.onload = () => setPrompt(String(r.result ?? ""));
    r.readAsText(f);
    e.target.value = "";
  };

  const generate = async () => {
    if (!up || !prompt.trim() || !model) return;
    setErr(null);
    const [w, h] = aspect.split("x").map(Number);
    try {
      await fetchJSON<{ id: string }>("/api/comfyui/generate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ prompt, model, width: w, height: h }),
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e)); // 409 = one already running
    }
  };

  const sendToTawhan = async () => {
    if (!prompt.trim()) return;
    setErr(null);
    try {
      await fetchJSON("/api/terminal/insert", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text: `Generate an image with ComfyUI: ${prompt}` }),
      });
      const tmux = useStore.getState().config?.modules.find((m) => m.id === "tmux");
      if (tmux) useStore.getState().setActive(tmux.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  // Machine has no GPU / ComfyUI disabled — render only the notice.
  if (state && state.available === false) {
    return (
      <div className="flex h-full w-full items-center justify-center p-6">
        <div className="hud hud--bracket reveal reveal-1 flex max-w-md flex-col items-center gap-2 p-6 text-center">
          <span className="panel-title">COMFYUI NOT AVAILABLE ON THIS MACHINE</span>
          <span className="mono text-sm text-muted">{state.reason ?? "see server logs"}</span>
        </div>
      </div>
    );
  }

  // Backend already drops entries that don't fit this machine (fit != "ok");
  // here we additionally hide unrestricted checkpoints unless toggled on.
  const byModality = (mod: "image" | "video") =>
    catalog.filter((e) => e.modality === mod && (showUnrestricted || !e.unrestricted));
  const canGen = up && prompt.trim().length > 0 && model.length > 0;
  const live = jobs.some((j) => j.status === "queued" || j.status === "running");
  const composer: { lbl: string; val: string; set: (v: string) => void }[] = [
    { lbl: "SUBJECT", val: subject, set: setSubject },
    { lbl: "MOOD", val: mood, set: setMood },
    { lbl: "STYLE", val: stylev, set: setStylev },
    { lbl: "LIGHTING", val: lighting, set: setLighting },
  ];

  return (
    <div className="flex h-full w-full flex-col gap-2 overflow-auto p-3">

      {/* HEADER — service state + VRAM + start/stop */}
      <div className="hud hud--bracket reveal reveal-1 flex flex-wrap items-center gap-2 p-3">
        <span className="panel-title">COMFY LAB</span>
        <span className={pipCls} />
        <span className="label">{up ? "ONLINE" : starting ? "STARTING" : "OFFLINE"}</span>
        {up && (
          <span className="pct" style={{ color: "var(--color-signal)" }}>
            {state!.vram_free_gb}/{state!.vram_total_gb} GiB
          </span>
        )}
        {live && <span className="label text-signal">BUSY</span>}
        <span className="flex-1" />
        <button
          className="btn"
          style={showUnrestricted ? {
            borderColor: "var(--color-hazard)",
            color: "var(--color-hazard)",
            background: "color-mix(in srgb, var(--color-hazard) 10%, var(--color-panel))",
          } : undefined}
          onClick={() => setShowUnrestricted((v) => !v)}
          title="Show/hide unrestricted (uncensored) checkpoints in the Model Bay"
        >
          [UNRESTRICTED]
        </button>
        {!up && (
          <button className="btn btn--signal" disabled={starting} onClick={start}>
            {starting ? "STARTING…" : "START"}
          </button>
        )}
        {up && (
          <button className="btn btn--crit" onClick={stop}>STOP</button>
        )}
      </div>

      {/* MODEL BAY — downloader */}
      <div className="hud hud--bracket reveal reveal-2 flex flex-col gap-2 p-3">
        <span className="label">MODEL BAY</span>
        {catalog.length === 0 && <span className="label text-muted">no models in catalog</span>}
        {(["image", "video"] as const).map((mod) => {
          const rows = byModality(mod);
          if (rows.length === 0) return null;
          return (
            <div key={mod} className="flex flex-col gap-0.5">
              <span className="label text-muted">{mod}</span>
              {rows.map((e) => {
                // Backend only sends fit == "ok" entries; proven models keep the glow.
                const entryJobs = jobs.filter((j) => j.kind === "download" && j.entry_id === e.id);
                const liveJobs = entryJobs.filter((j) => j.status === "queued" || j.status === "running");
                const inFlight = liveJobs.length > 0;
                const runningNow = liveJobs.some((j) => j.status === "running");
                return (
                  <div
                    key={e.id}
                    className="row-in flex flex-col gap-1 border border-edge bg-void px-2 py-1"
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className={`${e.proven ? "mono glow-go" : "mono"} flex-1 truncate`}
                        title={`${e.family} · ${e.variant}`}
                      >
                        {e.name}
                      </span>
                      {e.unrestricted && (
                        <span className="label whitespace-nowrap" style={{ color: "var(--color-hazard)" }}>
                          UNRESTRICTED
                        </span>
                      )}
                      <span className="mono text-xs text-muted whitespace-nowrap">
                        {e.size_gb} GB · {e.components.length} {e.components.length === 1 ? "file" : "files"}
                      </span>
                      {e.installed ? (
                        <span className="flex items-center gap-1 whitespace-nowrap">
                          <span className="label" style={{ color: "var(--color-go)" }}>✓ INSTALLED</span>
                          <button
                            className="btn btn--crit"
                            title="Delete this model's files from disk"
                            onClick={() => {
                              if (window.confirm(`Delete ${e.name} and its files? Re-download anytime.`)) uninstall(e.id);
                            }}
                          >
                            DELETE
                          </button>
                        </span>
                      ) : inFlight ? (
                        <button
                          className="btn btn--crit whitespace-nowrap"
                          title="Cancel this model's queued/running downloads"
                          onClick={() => cancelEntry(e.id)}
                        >
                          {runningNow ? "DOWNLOADING — CANCEL" : "QUEUED — CANCEL"}
                        </button>
                      ) : (
                        <button
                          className="btn whitespace-nowrap"
                          title="Download every missing file this model needs"
                          onClick={() => download(e.id)}
                        >
                          DOWNLOAD
                        </button>
                      )}
                    </div>
                    {/* Every file this entry needs to run, with per-file state. */}
                    <div className="flex flex-col gap-0.5 pl-3">
                      {e.components.map((c) => {
                        const key = `${c.folder}/${c.filename}`;
                        const cj =
                          entryJobs.find((j) => j.label === key && (j.status === "queued" || j.status === "running")) ??
                          entryJobs.find((j) => j.label === key);
                        const st = c.installed ? "done" : cj?.status ?? "missing";
                        const active = st === "queued" || st === "running";
                        return (
                          <div key={key} className="flex items-center gap-2">
                            <Pip status={st} />
                            <span className="mono flex-1 truncate text-xs" title={c.url}>{key}</span>
                            <span className="mono text-xs text-muted whitespace-nowrap">{c.size_gb} GB</span>
                            <span
                              className="label whitespace-nowrap"
                              style={{
                                color:
                                  c.installed ? "var(--color-go)" :
                                  st === "error" ? "var(--color-critical)" :
                                  active ? "var(--color-signal)" : "var(--color-muted)",
                              }}
                            >
                              {c.installed
                                ? "INSTALLED"
                                : cj
                                  ? `${cj.status.toUpperCase()}${active ? ` · ${cj.progress}%` : ""}`
                                  : "MISSING"}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>

      {/* GENERATE — mode + model + aspect */}
      <div className="hud hud--bracket reveal reveal-3 flex flex-col gap-2 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="label">MODE</span>
          <button className="btn btn--signal">IMAGE</button>
          <button className="btn" disabled title="v2">VIDEO</button>
          <span className="flex-1" />
        </div>
        <label className="flex items-center gap-2">
          <span className="label whitespace-nowrap">MODEL</span>
          <select
            className="input"
            value={model}
            onChange={(e) => { setModel(e.target.value); localStorage.setItem("comfy.model", e.target.value); }}
          >
            {installed.image.length === 0 && <option value="">(no image models installed)</option>}
            {installed.image.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <div className="flex flex-wrap items-center gap-2">
          <span className="label">ASPECT</span>
          {ASPECTS.map((a) => (
            <button
              key={a.v}
              className={aspect === a.v ? "btn btn--signal" : "btn"}
              onClick={() => { setAspect(a.v); localStorage.setItem("comfy.aspect", a.v); }}
            >
              {a.label}
            </button>
          ))}
        </div>
      </div>

      {/* PROMPT — composer / expand / attach / generate */}
      <div className="hud hud--bracket reveal reveal-4 flex flex-col gap-2 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="label">PROMPT</span>
          <button
            className={aiFill ? "btn btn--signal" : "btn"}
            onClick={() => setAiFill((v) => !v)}
          >
            AI FILL
          </button>
          <button className="btn" onClick={expand} disabled={expanding || !(aiFill ? subject : prompt).trim()}>
            EXPAND{expanding && <span className="caret" />}
          </button>
          <button
            className={confident ? "btn btn--signal" : "btn"}
            onClick={() => {
              const next = !confident;
              setConfident(next);
              localStorage.setItem("comfy.confident", next ? "on" : "off");
            }}
            title="ON = fire /generate directly; OFF = route via TERMINAL"
          >
            CONFIDENT
          </button>
          <button className="btn" onClick={() => document.getElementById("comfy-txt-attach")?.click()}>
            ATTACH .TXT
          </button>
          <input id="comfy-txt-attach" type="file" accept=".txt" className="hidden" onChange={onAttachTxt} />
        </div>

        {aiFill && (
          <div className="grid grid-cols-2 gap-1">
            {composer.map((f) => (
              <label key={f.lbl} className="flex items-center gap-1">
                <span className="label whitespace-nowrap text-muted">{f.lbl}</span>
                <input className="input" value={f.val} onChange={(e) => f.set(e.target.value)} />
              </label>
            ))}
            <button className="btn btn--signal col-span-2" onClick={compose} disabled={!subject && !mood && !stylev && !lighting}>
              COMPOSE
            </button>
          </div>
        )}

        <textarea
          className="input"
          rows={4}
          placeholder="describe the frame…"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />

        <div className="flex items-center gap-2">
          <button className="btn btn--signal flex-1" disabled={!canGen} onClick={generate}>
            GENERATE
          </button>
          {!confident && (
            <button className="btn" disabled={!prompt.trim()} onClick={sendToTawhan}>SEND TO TAWHAN</button>
          )}
        </div>
        {err && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{err}</div>}
      </div>

      {/* JOBS — downloads + generation, live */}
      <div className="hud hud--bracket reveal reveal-5 flex flex-col gap-2 p-3">
        <span className="label">JOBS</span>
        {jobs.length === 0 && <span className="label text-muted">no jobs yet</span>}
        {jobs.map((j) => {
          const tail = j.logs.slice(-10);
          const fill =
            j.status === "done" ? "var(--color-go)" :
            j.status === "error" ? "var(--color-critical)" :
            j.status === "cancelled" ? "var(--color-hazard)" :
            "var(--color-signal)";
          return (
            <div key={j.id} className="border border-edge bg-void p-2">
              <div className="flex items-center gap-2">
                <Pip status={j.status} />
                <span className="mono">{j.kind}</span>
                <span className="label truncate flex-1" title={j.label}>{j.label}</span>
                <span className="label whitespace-nowrap">{j.status.toUpperCase()} · {j.progress}%</span>
                {(j.status === "running" || j.status === "queued") && (
                  <button
                    className="btn btn--crit"
                    onClick={() => fetchJSON(`/api/comfyui/jobs/${j.id}/cancel`, { method: "POST" }).catch(() => {})}
                  >
                    CANCEL
                  </button>
                )}
              </div>
              <div className="mt-1 h-1.5 w-full bg-edge-soft">
                <div style={{ width: `${j.progress}%`, height: "100%", background: fill }} />
              </div>
              {j.error && <div className="mono mt-1 text-xs" style={{ color: "var(--color-critical)" }}>{j.error}</div>}
              {tail.length > 0 && (
                <pre className="mono mt-1 max-h-24 overflow-auto whitespace-pre-wrap text-xs text-muted">{tail.join("\n")}</pre>
              )}
            </div>
          );
        })}
      </div>

      {/* OUTPUT FEED — newest first */}
      <div className="hud hud--bracket reveal reveal-5 flex flex-col gap-2 p-3">
        <span className="label">OUTPUT FEED</span>
        {files.length === 0 && <span className="label text-muted">no outputs yet</span>}
        <div className="grid grid-cols-4 gap-2">
          {files.map((f) => (
            <a key={f.path} href={fileUrl(f.path)} target="_blank" rel="noreferrer" title={f.name}>
              <img
                src={fileUrl(f.path)}
                alt={f.name}
                loading="lazy"
                className="aspect-square w-full border border-edge object-cover"
              />
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
