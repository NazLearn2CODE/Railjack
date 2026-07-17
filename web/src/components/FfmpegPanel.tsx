import { useEffect, useState } from "react";
import { fetchJSON, usePolling } from "../api";
import type { ModuleConfig } from "../store";

/**
 * Video Lab — ffmpeg op runner. Op dropdown + ordered multi-select clips +
 * op params + RUN, then a live job list (progress bar + log tail). Polls
 * /api/ffmpeg/jobs every 1 s while mounted (the panel only mounts for the
 * active module, so "mounted" ≈ "visible"). One job runs at a time server-side.
 */

const OPS: { id: string; label: string; needs: "single" | "multi"; hint: string }[] = [
  { id: "transcode_h264", label: "TRANSCODE → H.264", needs: "single", hint: "one clip → CRF 18 master" },
  { id: "concat", label: "STITCH (concat)", needs: "multi", hint: "≥2 clips, in order" },
  { id: "lut", label: "APPLY LUT", needs: "single", hint: "one clip + a .cube LUT" },
  { id: "xfade", label: "CROSSFADE (xfade)", needs: "multi", hint: "≥2 clips, in order" },
  { id: "transcode_dnxhr", label: "TRANSCODE → DNxHR", needs: "single", hint: "one clip → Resolve edit intermediate" },
];

interface FileEntry { root: string; name: string; path: string }
interface LutEntry { name: string; path: string }
interface Job {
  id: string; op: string; status: string; progress: number;
  output_path: string | null; error: string | null; logs: string[];
}

function Pip({ status }: { status: string }) {
  const cls =
    status === "done" ? "pip pip--go" :
    status === "error" ? "pip pip--crit" :
    status === "running" ? "pip pip--signal" :
    status === "cancelled" ? "pip pip--hazard" : "pip";
  return <span className={cls} />;
}

export default function FfmpegPanel({ module }: { module: ModuleConfig }) {
  const [op, setOp] = useState(OPS[0].id);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [luts, setLuts] = useState<LutEntry[]>([]);
  const [selected, setSelected] = useState<string[]>([]); // ordered paths
  const [transition, setTransition] = useState(0.5);
  const [lutPath, setLutPath] = useState<string>("");
  const [runErr, setRunErr] = useState<string | null>(null);

  useEffect(() => {
    fetchJSON<{ files: FileEntry[] }>("/api/ffmpeg/files").then((r) => setFiles(r.files)).catch(() => setFiles([]));
    fetchJSON<{ luts: LutEntry[] }>("/api/ffmpeg/luts").then((r) => {
      setLuts(r.luts);
      if (r.luts[0]) setLutPath(r.luts[0].path);
    }).catch(() => setLuts([]));
  }, []);

  const { data } = usePolling<{ jobs: Job[] }>("/api/ffmpeg/jobs", 1000);
  const jobs = data?.jobs ?? [];
  const live = jobs.some((j) => j.status === "queued" || j.status === "running");

  const meta = OPS.find((o) => o.id === op)!;
  const canRun =
    !live &&
    (meta.needs === "multi" ? selected.length >= 2 : selected.length === 1) &&
    (op !== "lut" || Boolean(lutPath));

  const toggle = (path: string) =>
    setSelected((cur) => (cur.includes(path) ? cur.filter((p) => p !== path) : [...cur, path]));

  // clear selection that no longer fits the op's arity is the user's call —
  // the RUN button simply won't enable. Keep order stable on toggle-off.

  const run = async () => {
    setRunErr(null);
    const body: Record<string, unknown> = { op, files: selected };
    if (op === "xfade") body.transition = transition;
    if (op === "lut") body.lut = lutPath;
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

  return (
    <div className="flex h-full w-full flex-col gap-2 overflow-auto p-3">
      <div className="flex items-center gap-2 px-1">
        <span className="panel-title">{module.title}</span>
        {live && <span className="label text-signal">RUNNING</span>}
      </div>

      {/* OP + params + RUN */}
      <div className="hud hud--bracket reveal reveal-3 flex flex-col gap-2 p-3">
        <div className="flex items-center gap-2">
          <span className="label">OP</span>
          <select className="input flex-1" value={op} onChange={(e) => setOp(e.target.value)}>
            {OPS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
          <span className="label text-muted">{meta.hint}</span>
        </div>

        {op === "xfade" && (
          <label className="flex items-center gap-2">
            <span className="label">TRANSITION (s)</span>
            <input
              className="input w-24"
              type="number" min={0.1} step={0.1} value={transition}
              onChange={(e) => setTransition(Math.max(0.1, Number(e.target.value) || 0.5))}
            />
          </label>
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
          {files.length === 0 && <span className="label text-muted">no video files in media dirs</span>}
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
    </div>
  );
}
