import { useCallback, useEffect, useRef, useState } from "react";
import type { ModuleConfig } from "../store";

// CODEATLAS — named-target architecture maps (Naz + Damriw dispatch 2026-09-24).
// Name a Coding Project / skill / Railjack (optionally focused on one flow) →
// the agy lane writes a single-file CodeAtlas map INTO <target>/CODEATLAS/ and
// this panel serves it + tracks staleness against the target's HEAD.

interface AtlasJobState {
  id: string;
  status: "queued" | "running" | "done" | "error" | "cancelled";
  progress: number;
  label: string;
  logs: string[];
  error?: string | null;
  target_path: string;
  result?: { validated?: boolean; head?: string | null; focus?: string; mapdir?: string; map_url?: string } | null;
}

interface CheckState {
  target: string;
  state: "missing" | "current" | "stale";
  mapped_head?: string | null;
  head?: string | null;
  age_days?: number;
  focus?: string;
}

async function post<T>(url: string, body: unknown): Promise<{ ok: boolean; data?: T; error?: string }> {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, error: typeof data.detail === "string" ? data.detail : res.statusText };
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : String(e) };
  }
}

export default function CodeAtlasPanel({ module }: { module: ModuleConfig }) {
  const [target, setTarget] = useState("");
  const [focus, setFocus] = useState("");
  const [job, setJob] = useState<AtlasJobState | null>(null);
  const [check, setCheck] = useState<CheckState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const poll = useCallback(async (jid: string) => {
    const r = await fetch(`/api/codeatlas/job/${jid}`);
    if (!r.ok) return;
    const d = (await r.json()) as AtlasJobState;
    setJob(d);
    if (d.status === "done" || d.status === "error" || d.status === "cancelled") {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
      // refresh staleness now that a map may exist
      const c = await fetch(`/api/codeatlas/check?target=${encodeURIComponent(target)}`);
      if (c.ok) setCheck((await c.json()) as CheckState);
    }
  }, [target]);

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const generate = async () => {
    if (!target.trim()) { setError("name a target first"); return; }
    setBusy(true); setError(null); setJob(null); setCheck(null);
    const r = await post<{ id: string }>("/api/codeatlas/generate", { target: target.trim(), focus: focus.trim() });
    if (!r.ok || !r.data?.id) {
      setError(r.error || "generate failed");
      setBusy(false);
      return;
    }
    setBusy(false);
    const jid = r.data.id;
    void poll(jid);
    pollRef.current = window.setInterval(() => void poll(jid), 4000);
  };

  const runCheck = async () => {
    if (!target.trim()) { setError("name a target first"); return; }
    setError(null);
    const c = await fetch(`/api/codeatlas/check?target=${encodeURIComponent(target.trim())}`);
    if (c.ok) setCheck((await c.json()) as CheckState);
  };

  const badgeColor =
    check?.state === "current" ? "var(--color-go)"
    : check?.state === "stale" ? "var(--color-hazard)"
    : "var(--color-muted)";

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-auto p-3">
      <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
        {module.title} — name a Coding Project, skill, or <span style={{ color: "var(--color-phosphor)" }}>railjack</span>
        {" "}→ agy maps it into the target's own <span style={{ color: "var(--color-phosphor)" }}>CODEATLAS/</span> folder.
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <input
          className="mono text-xs"
          style={{ padding: "6px 8px", border: "1px solid var(--color-edge)", background: "var(--color-void)", color: "var(--color-fg)", minWidth: 220 }}
          placeholder="target — e.g. agent-x · Railjack · newsroom"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        />
        <input
          className="mono text-xs"
          style={{ padding: "6px 8px", border: "1px solid var(--color-edge)", background: "var(--color-void)", color: "var(--color-fg)", minWidth: 200 }}
          placeholder="focus (optional) — e.g. IDE SCOUT"
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
        />
        <button className="btn btn--compact" disabled={busy || !target.trim()} onClick={() => void generate()}>
          {busy ? "launching…" : "GENERATE MAP"}
        </button>
        <button className="btn btn--compact" disabled={!target.trim()} onClick={() => void runCheck()}>
          CHECK
        </button>
      </div>

      {error && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{error}</div>}

      {check && (
        <div className="mono text-xs flex flex-wrap items-center gap-2" style={{ color: badgeColor }}>
          <span style={{ fontWeight: 700 }}>
            {check.state === "current" ? "✓ CURRENT" : check.state === "stale" ? "⚠ STALE" : "NO MAP"}
          </span>
          {typeof check.age_days === "number" && <span>mapped {check.age_days}d ago</span>}
          {check.focus && <span>focus: {check.focus}</span>}
          {check.state !== "missing" && (
            <a
              className="btn btn--compact"
              href={`/api/codeatlas/open-map?target=${encodeURIComponent(target.trim())}`}
              target="_blank"
              rel="noreferrer"
            >
              OPEN MAP ↗
            </a>
          )}
        </div>
      )}

      {job && (
        <div className="mono text-xs flex flex-col gap-1" style={{ border: "1px solid var(--color-edge)", padding: 8 }}>
          <div>
            {job.status === "done" ? "✓" : job.status === "error" ? "✕" : "⟳"} {job.status} · {job.label} · {job.progress}%
          </div>
          {job.logs.map((l, i) => (
            <div key={i} style={{ color: "var(--color-muted)" }}>{l}</div>
          ))}
          {job.error && <div style={{ color: "var(--color-critical)" }}>{job.error}</div>}
          {job.status === "done" && job.result?.map_url && (
            <a className="btn btn--compact" style={{ justifySelf: "start" }} href={job.result.map_url} target="_blank" rel="noreferrer">
              OPEN THE MAP ↗
            </a>
          )}
        </div>
      )}
    </div>
  );
}
