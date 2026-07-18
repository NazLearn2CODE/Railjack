import { useCallback, useEffect, useState } from "react";
import { fetchJSON, usePolling } from "../api";
import type { ModuleConfig } from "../store";

/**
 * Video Lab — ffmpeg op runner. Op dropdown + ordered multi-select clips +
 * op params + RUN, then a live job list (progress bar + log tail). Polls
 * /api/ffmpeg/jobs every 1 s while mounted (the panel only mounts for the
 * active module, so "mounted" ≈ "visible"). One job runs at a time server-side.
 *
 * M6.2: a baked-in file browser. FOOTAGE folders (persisted per browser) seed
 * the CLIPS list via /api/ffmpeg/files?dir=…; ADD FOOTAGE opens a folder picker
 * confined to the server's browse root (~). OUTPUT folder is likewise pickable
 * (default ~/Downloads/VDO Outputs) and travels with the RUN request.
 */

/**
 * Op catalog is backend-driven: /api/ffmpeg/ops is the single source of truth
 * (an import-time assert keeps it synced with BUILDERS). The menu is grouped
 * client-side by `cat`, so adding a no-param effect in the backend grows this
 * menu with zero frontend edit. FALLBACK_OPS only covers a fetch failure so the
 * panel still renders.
 */
interface Op { id: string; label: string; needs: "single" | "multi"; hint: string; cat: string }

const FALLBACK_OPS: Op[] = [
  { id: "transcode_h264", label: "H.264 master", needs: "single", hint: "CRF 18 master", cat: "TRANSCODE" },
];

interface FileEntry { root: string; name: string; path: string }
interface LutEntry { name: string; path: string }
interface PanelCfg { browse_root: string; default_output: string; media_dirs: string[] }
interface BrowseResp { path: string; root: string; parent: string | null; dirs: { name: string; path: string }[]; video_count: number }
interface Job {
  id: string; op: string; status: string; progress: number;
  output_path: string | null; error: string | null; logs: string[];
}

const WS_KEY = "railjack.ffmpeg.workspace";
const OUT_KEY = "railjack.ffmpeg.output";

function loadLS<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}
function saveLS(key: string, val: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(val));
  } catch {
    /* private mode / quota — non-fatal, workspace just won't persist */
  }
}
const baseName = (p: string) => p.replace(/\/+$/, "").split("/").pop() || p;

function Pip({ status }: { status: string }) {
  const cls =
    status === "done" ? "pip pip--go" :
    status === "error" ? "pip pip--crit" :
    status === "running" ? "pip pip--signal" :
    status === "cancelled" ? "pip pip--hazard" : "pip";
  return <span className={cls} />;
}

/** Folder picker overlay. Navigates sub-folders under the server browse root;
 * commits the currently-open folder via onPick. Used for both ADD FOOTAGE and
 * choosing the OUTPUT folder (mode only changes the labels). */
function FolderBrowser({
  mode, start, onPick, onClose,
}: { mode: "footage" | "output"; start: string; onPick: (p: string) => void; onClose: () => void }) {
  const [path, setPath] = useState<string | null>(start || null);
  const [data, setData] = useState<BrowseResp | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const url = path ? `/api/ffmpeg/browse?path=${encodeURIComponent(path)}` : "/api/ffmpeg/browse";
    fetchJSON<BrowseResp>(url)
      .then((r) => { setData(r); setErr(null); if (path === null) setPath(r.path); })
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [path]);

  const commit = mode === "footage" ? "ADD THIS FOLDER" : "USE THIS FOLDER";

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-6" style={{ background: "color-mix(in srgb, #000 70%, transparent)" }}>
      <div className="hud hud--bracket flex max-h-[80vh] w-full max-w-2xl flex-col gap-2 p-3">
        <div className="flex items-center gap-2">
          <span className="panel-title">{mode === "footage" ? "ADD FOOTAGE" : "OUTPUT FOLDER"}</span>
          <span className="flex-1" />
          <button className="btn" onClick={onClose}>CLOSE</button>
        </div>
        <div className="mono truncate text-xs text-muted" title={data?.path}>{data?.path ?? "…"}</div>
        {err && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{err}</div>}

        <div className="flex min-h-40 flex-col gap-0.5 overflow-auto border border-edge bg-void p-1">
          {data?.parent && (
            <button className="row-in flex items-center gap-2 px-1 py-0.5 text-left" onClick={() => setPath(data.parent)}>
              <span className="mono w-4 text-signal">↑</span>
              <span className="mono flex-1 truncate">.. (up)</span>
            </button>
          )}
          {data && data.dirs.length === 0 && <span className="label text-muted px-1 py-1">no sub-folders here</span>}
          {data?.dirs.map((d) => (
            <button key={d.path} className="row-in flex items-center gap-2 px-1 py-0.5 text-left" onClick={() => setPath(d.path)}>
              <span className="mono w-4 text-muted">▸</span>
              <span className="mono flex-1 truncate">{d.name}</span>
            </button>
          ))}
        </div>

        <div className="flex items-center gap-2">
          <span className="label text-muted flex-1">
            {data ? `${data.video_count} video${data.video_count === 1 ? "" : "s"} directly here` : ""}
          </span>
          <button
            className="btn btn--signal"
            disabled={!data}
            onClick={() => data && onPick(data.path)}
          >
            {commit}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function FfmpegPanel({ module }: { module: ModuleConfig }) {
  const [catalog, setCatalog] = useState<Op[]>(FALLBACK_OPS);
  const [op, setOp] = useState("transcode_h264");
  const [cfg, setCfg] = useState<PanelCfg | null>(null);
  const [workspace, setWorkspace] = useState<string[] | null>(null); // null = not yet initialised
  const [outputDir, setOutputDir] = useState<string>("");
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [luts, setLuts] = useState<LutEntry[]>([]);
  const [selected, setSelected] = useState<string[]>([]); // ordered paths
  const [transition, setTransition] = useState(0.5);
  const [xstyle, setXstyle] = useState("fade");
  const [transitions, setTransitions] = useState<string[]>(["fade"]);
  const [fadeDur, setFadeDur] = useState(0.5);
  const [speedFac, setSpeedFac] = useState(2);
  const [gain, setGain] = useState(2);
  const [lutPath, setLutPath] = useState<string>("");
  const [runErr, setRunErr] = useState<string | null>(null);
  const [browser, setBrowser] = useState<"footage" | "output" | null>(null);

  // Bootstrap: panel config + LUTs. Seed workspace/output from saved state,
  // falling back to the server's media_dirs / default output.
  useEffect(() => {
    fetchJSON<PanelCfg>("/api/ffmpeg/panel").then((c) => {
      setCfg(c);
      const savedWs = loadLS<string[]>(WS_KEY);
      setWorkspace(savedWs ?? c.media_dirs);
      const savedOut = loadLS<string>(OUT_KEY);
      setOutputDir(savedOut ?? c.default_output);
    }).catch(() => { setCfg({ browse_root: "~", default_output: "", media_dirs: [] }); setWorkspace([]); });

    fetchJSON<{ luts: LutEntry[] }>("/api/ffmpeg/luts").then((r) => {
      setLuts(r.luts);
      if (r.luts[0]) setLutPath(r.luts[0].path);
    }).catch(() => setLuts([]));

    fetchJSON<{ ops: Op[] }>("/api/ffmpeg/ops").then((r) => {
      if (r.ops.length) setCatalog(r.ops);
    }).catch(() => {});
    fetchJSON<{ transitions: string[] }>("/api/ffmpeg/transitions").then((r) => {
      if (r.transitions.length) setTransitions(r.transitions);
    }).catch(() => {});
  }, []);

  // Clips = videos under the workspace folders. Refetch when the set changes.
  const refetchFiles = useCallback((dirs: string[]) => {
    if (dirs.length === 0) { setFiles([]); return; }
    const q = dirs.map((d) => `dir=${encodeURIComponent(d)}`).join("&");
    fetchJSON<{ files: FileEntry[] }>(`/api/ffmpeg/files?${q}`).then((r) => setFiles(r.files)).catch(() => setFiles([]));
  }, []);

  useEffect(() => {
    if (workspace === null) return; // not initialised yet
    saveLS(WS_KEY, workspace);
    refetchFiles(workspace);
  }, [workspace, refetchFiles]);

  const addFolder = (p: string) => {
    setWorkspace((cur) => (cur && cur.includes(p) ? cur : [...(cur ?? []), p]));
    setBrowser(null);
  };
  const removeFolder = (p: string) =>
    setWorkspace((cur) => (cur ?? []).filter((d) => d !== p));

  const pickOutput = (p: string) => {
    setOutputDir(p);
    saveLS(OUT_KEY, p);
    setBrowser(null);
  };

  const { data } = usePolling<{ jobs: Job[] }>("/api/ffmpeg/jobs", 1000);
  const jobs = data?.jobs ?? [];
  const live = jobs.some((j) => j.status === "queued" || j.status === "running");

  const meta = catalog.find((o) => o.id === op) ?? FALLBACK_OPS[0];
  const cats = Array.from(new Set(catalog.map((o) => o.cat))); // order of first appearance
  const canRun =
    !live &&
    (meta.needs === "multi" ? selected.length >= 2 : selected.length === 1) &&
    (op !== "lut" || Boolean(lutPath));

  const toggle = (path: string) =>
    setSelected((cur) => (cur.includes(path) ? cur.filter((p) => p !== path) : [...cur, path]));

  const run = async () => {
    setRunErr(null);
    const body: Record<string, unknown> = { op, files: selected, output_dir: outputDir || undefined };
    if (op === "xfade") { body.transition = transition; body.transition_style = xstyle; }
    if (op === "lut") body.lut = lutPath;
    if (op === "fade") body.fade = fadeDur;
    if (op === "speed") body.speed = speedFac;
    if (op === "volume") body.gain = gain;
    try {
      await fetchJSON<{ id: string }>("/api/ffmpeg/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      setSelected([]);
    } catch (e) {
      setRunErr(e instanceof Error ? e.message : String(e));
    }
  };

  const ws = workspace ?? [];

  return (
    <div className="flex h-full w-full flex-col gap-2 overflow-auto p-3">
      <div className="flex items-center gap-2 px-1">
        <span className="panel-title">{module.title}</span>
        {live && <span className="label text-signal">RUNNING</span>}
      </div>

      {/* FOOTAGE folders + OUTPUT */}
      <div className="hud hud--bracket reveal reveal-2 flex flex-col gap-2 p-3">
        <div className="flex items-center gap-2">
          <span className="label">FOOTAGE</span>
          <span className="flex-1" />
          <button className="btn btn--signal" onClick={() => setBrowser("footage")}>ADD FOOTAGE</button>
        </div>
        <div className="flex flex-wrap gap-1">
          {ws.length === 0 && <span className="label text-muted">no folders — click ADD FOOTAGE</span>}
          {ws.map((d) => (
            <span key={d} className="mono flex items-center gap-1 border border-edge bg-void px-1.5 py-0.5 text-xs" title={d}>
              {baseName(d)}
              <button className="text-muted hover:text-signal" onClick={() => removeFolder(d)} title="remove">×</button>
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <span className="label">OUTPUT</span>
          <span className="mono flex-1 truncate text-xs text-muted" title={outputDir}>{outputDir || "…"}</span>
          <button className="btn" onClick={() => setBrowser("output")}>CHANGE</button>
        </div>
      </div>

      {/* OP + params + RUN */}
      <div className="hud hud--bracket reveal reveal-3 flex flex-col gap-2 p-3">
        {/* one <select> per category; all share `op`, so picking in one moves it */}
        <div className="flex flex-wrap items-center gap-2">
          {cats.map((cat) => {
            const opts = catalog.filter((o) => o.cat === cat);
            return (
              <label key={cat} className="flex items-center gap-1">
                <span className="label whitespace-nowrap text-muted">{cat}</span>
                <select
                  className="input"
                  value={opts.some((o) => o.id === op) ? op : ""}
                  onChange={(e) => e.target.value && setOp(e.target.value)}
                >
                  <option value="">—</option>
                  {opts.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                </select>
              </label>
            );
          })}
        </div>
        <span className="label text-muted">{meta.hint}</span>

        {op === "xfade" && (
          <>
            <label className="flex items-center gap-2">
              <span className="label">TRANSITION</span>
              <select className="input flex-1" value={xstyle} onChange={(e) => setXstyle(e.target.value)}>
                {transitions.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
            <label className="flex items-center gap-2">
              <span className="label">DURATION (s)</span>
              <input
                className="input w-24"
                type="number" min={0.1} step={0.1} value={transition}
                onChange={(e) => setTransition(Math.max(0.1, Number(e.target.value) || 0.5))}
              />
            </label>
          </>
        )}

        {op === "lut" && (
          <label className="flex items-center gap-2">
            <span className="label">LUT</span>
            <select className="input flex-1" value={lutPath} onChange={(e) => setLutPath(e.target.value)}>
              {luts.length === 0 && <option value="">(no LUTs found)</option>}
              {luts.map((l) => <option key={l.path} value={l.path}>{l.name}</option>)}
            </select>
          </label>
        )}

        {op === "fade" && (
          <label className="flex items-center gap-2">
            <span className="label">FADE (s)</span>
            <input className="input w-24" type="number" min={0.1} step={0.1} value={fadeDur}
              onChange={(e) => setFadeDur(Math.max(0.1, Number(e.target.value) || 0.5))} />
          </label>
        )}

        {op === "speed" && (
          <label className="flex items-center gap-2">
            <span className="label">SPEED (×)</span>
            <input className="input w-24" type="number" min={0.25} max={16} step={0.25} value={speedFac}
              onChange={(e) => setSpeedFac(Math.min(16, Math.max(0.25, Number(e.target.value) || 2)))} />
          </label>
        )}

        {op === "volume" && (
          <label className="flex items-center gap-2">
            <span className="label">GAIN (×)</span>
            <input className="input w-24" type="number" min={0.1} step={0.1} value={gain}
              onChange={(e) => setGain(Math.max(0.1, Number(e.target.value) || 2))} />
          </label>
        )}

        <div className="flex items-center gap-2">
          <span className="flex-1" />
          <span className="label">{selected.length} SELECTED</span>
          <button className="btn" onClick={() => setSelected([])} disabled={!selected.length}>CLEAR</button>
          <button className="btn btn--signal" onClick={run} disabled={!canRun}>RUN</button>
        </div>
        {runErr && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{runErr}</div>}
      </div>

      {/* CLIPS — ordered multi-select */}
      <div className="hud hud--bracket reveal reveal-4 flex min-h-32 flex-col gap-1 p-2">
        <span className="label mb-1">
          CLIPS{meta.needs === "multi" ? " — order = stitch/crossfade order" : ""}
        </span>
        <div className="flex flex-col gap-0.5 overflow-auto">
          {files.length === 0 && (
            <span className="label text-muted">
              {ws.length === 0 ? "add a footage folder to see clips" : "no video files in these folders"}
            </span>
          )}
          {files.map((f) => {
            const idx = selected.indexOf(f.path);
            const on = idx >= 0;
            return (
              <label key={f.path} className="row-in flex items-center gap-2 px-1 py-0.5">
                <input type="checkbox" checked={on} onChange={() => toggle(f.path)} />
                {on && <span className="mono w-5 text-signal">{idx + 1}</span>}
                {!on && <span className="mono w-5" />}
                <span className="mono flex-1 truncate">{f.name}</span>
                <span className="label text-muted">{f.root}</span>
              </label>
            );
          })}
        </div>
      </div>

      {/* JOBS */}
      <div className="hud hud--bracket reveal reveal-5 flex flex-col gap-2 p-3">
        <span className="label">JOBS</span>
        {jobs.length === 0 && <span className="label text-muted">no jobs yet</span>}
        {jobs.map((j) => {
          const tail = j.logs.slice(-10);
          const pct = j.progress;
          const fill =
            j.status === "done" ? "var(--color-go)" :
            j.status === "error" ? "var(--color-critical)" :
            j.status === "cancelled" ? "var(--color-hazard)" :
            "var(--color-signal)";
          return (
            <div key={j.id} className="border border-edge bg-void p-2">
              <div className="flex items-center gap-2">
                <Pip status={j.status} />
                <span className="mono">{j.op}</span>
                <span className="label">{j.status.toUpperCase()} · {pct}%</span>
                <span className="flex-1" />
                {(j.status === "running" || j.status === "queued") && (
                  <button
                    className="btn btn--crit"
                    onClick={() => fetchJSON(`/api/ffmpeg/jobs/${j.id}/cancel`, { method: "POST" }).catch(() => {})}
                  >
                    CANCEL
                  </button>
                )}
              </div>
              <div className="mt-1 h-1.5 w-full bg-edge-soft">
                <div style={{ width: `${pct}%`, height: "100%", background: fill }} />
              </div>
              {j.error && <div className="mono mt-1 text-xs" style={{ color: "var(--color-critical)" }}>{j.error}</div>}
              {j.output_path && j.status === "done" && (
                <div className="mono mt-1 truncate text-xs text-signal">{j.output_path}</div>
              )}
              {tail.length > 0 && (
                <pre className="mono mt-1 max-h-32 overflow-auto whitespace-pre-wrap text-xs text-muted">{tail.join("\n")}</pre>
              )}
            </div>
          );
        })}
      </div>

      {browser && (
        <FolderBrowser
          mode={browser}
          start={browser === "output" ? outputDir : (cfg?.browse_root ?? "")}
          onPick={browser === "output" ? pickOutput : addFolder}
          onClose={() => setBrowser(null)}
        />
      )}
    </div>
  );
}
