import { useEffect, useState, type CSSProperties } from "react";
import { fetchJSON, usePolling } from "../api";
import { useStore } from "../store";

/**
 * Cockpit controls + session telemetry. The dropdowns/buttons only *type* a
 * short string into the tmux session (POST /api/terminal/insert) — NO Enter —
 * then flip the active module to TERMINAL so the typed text is visible. Naz
 * reviews and presses Enter in the pane.
 *
 * Telemetry: /api/session polled every 15 s; the RESET countdown ticks locally
 * every 1 s off the shared clock between polls. ctx bar goes amber ≥70 % and
 * crit ≥90 % via the threshold → CSS var color (the pip dot carries the same
 * semantics using the existing pip--* classes).
 */

interface CatalogEntry {
  name: string;
  insert: string;
  group: string;
}
interface Catalog {
  skills: CatalogEntry[];
  mcps: CatalogEntry[];
}
interface Session {
  provider: string;
  model: string;
  context_tokens: number;
  context_limit: number;
  context_pct: number;
  session_pct: number | null;
  weekly_pct: number | null;
  reset_at: string | null;
  source: "api" | "estimate" | "none";
  idle: boolean;
}

const SELECT_STYLE: CSSProperties = {
  background: "var(--color-panel-2)",
  color: "var(--color-phosphor-dim)",
  border: "1px solid var(--color-edge)",
  padding: "4px 6px",
};

/** Group entries preserving first-appearance order (OTHER sinks to its place). */
function grouped(items: CatalogEntry[]): { name: string; items: CatalogEntry[] }[] {
  const order: string[] = [];
  const map = new Map<string, CatalogEntry[]>();
  for (const it of items) {
    if (!map.has(it.group)) {
      map.set(it.group, []);
      order.push(it.group);
    }
    map.get(it.group)!.push(it);
  }
  return order.map((name) => ({ name, items: map.get(name)! }));
}

function OptGroup({ name, items }: { name: string; items: CatalogEntry[] }) {
  return (
    <optgroup label={name}>
      {items.map((it) => (
        <option key={it.name} value={it.insert}>
          {it.name}
        </option>
      ))}
    </optgroup>
  );
}

export default function TopBar() {
  const machine = useStore((s) => s.config?.machine);
  const buttons = useStore((s) => s.config?.buttons) ?? [];
  const [now, setNow] = useState(() => new Date());
  const [skillSel, setSkillSel] = useState("");
  const [mcpSel, setMcpSel] = useState("");

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const { data: catalog } = usePolling<Catalog>("/api/catalog", 60_000);
  const { data: session } = usePolling<Session>("/api/session", 15_000);

  const insert = async (text: string) => {
    try {
      await fetchJSON("/api/terminal/insert", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const tmux = useStore.getState().config?.modules.find((m) => m.id === "tmux");
      if (tmux) useStore.getState().setActive(tmux.id);
    } catch (e) {
      console.error("terminal insert failed", e);
    }
  };

  const clock = now.toLocaleTimeString("en-GB"); // HH:MM:SS 24h

  const skills = grouped(catalog?.skills ?? []);
  const mcps = grouped(catalog?.mcps ?? []);

  // Telemetry thresholds.
  const pct = session?.context_pct ?? 0;
  const pipClass =
    pct >= 90 ? "pip pip--crit" : pct >= 70 ? "pip pip--hazard" : "pip pip--go";
  const barColor =
    pct >= 90
      ? "var(--color-critical)"
      : pct >= 70
        ? "var(--color-hazard)"
        : "var(--color-go)";
  // Session/weekly quota % (from the provider's official API; null on estimate).
  const sesPct = session?.session_pct ?? null;
  const wkPct = session?.weekly_pct ?? null;
  const sesStyle = (p: number): CSSProperties | undefined =>
    p >= 90
      ? { color: "var(--color-critical)" }
      : p >= 70
        ? { color: "var(--color-hazard)" }
        : undefined;
  const resetMs = session?.reset_at ? Date.parse(session.reset_at) : 0;
  const remaining = resetMs ? Math.max(0, resetMs - now.getTime()) : 0;
  const totalMin = Math.floor(remaining / 60_000);
  // "~" prefix marks the JSONL fallback estimate; API-sourced shows bare h:mm.
  const estMark = session?.source === "estimate" ? "~" : "";
  const resetLabel = resetMs
    ? `${estMark}${Math.floor(totalMin / 60)}:${String(totalMin % 60).padStart(2, "0")}`
    : "—";

  return (
    <header className="hud hud--bracket reveal reveal-1 m-2 mb-0 flex flex-wrap items-center justify-between gap-2 px-4 py-2">
      <div className="flex items-center gap-3">
        <span className="panel-title">▸ RAILJACK</span>
        <span className="label">// {(machine ?? "—").toUpperCase()}</span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <select
          className="mono label"
          style={SELECT_STYLE}
          value={skillSel}
          onChange={(e) => {
            const v = e.target.value;
            if (v) {
              void insert(v);
              setSkillSel("");
            }
          }}
        >
          <option value="" disabled>
            SKILLS
          </option>
          {skills.map((g) => (
            <OptGroup key={g.name} {...g} />
          ))}
        </select>

        <select
          className="mono label"
          style={SELECT_STYLE}
          value={mcpSel}
          onChange={(e) => {
            const v = e.target.value;
            if (v) {
              void insert(v);
              setMcpSel("");
            }
          }}
        >
          <option value="" disabled>
            MCP
          </option>
          {mcps.map((g) => (
            <OptGroup key={g.name} {...g} />
          ))}
        </select>

        {buttons.map((b) => (
          <button key={b.label} className="btn btn--signal" onClick={() => void insert(b.insert)}>
            {b.label}
          </button>
        ))}

        {/* Telemetry strip */}
        {session && !session.idle ? (
          <span className="flex items-center gap-2">
            <span className="label">{session.provider}</span>
            <span className="label">CTX</span>
            <span className={pipClass} aria-hidden />
            <span className="mono">{session.context_pct}%</span>
            <span
              aria-hidden
              style={{
                display: "inline-block",
                width: 44,
                height: 5,
                background: "var(--color-edge)",
                border: "1px solid var(--color-edge-soft)",
              }}
            >
              <span
                style={{
                  display: "block",
                  width: `${Math.min(100, session.context_pct)}%`,
                  height: "100%",
                  background: barColor,
                }}
              />
            </span>
            {sesPct !== null && (
              <>
                <span className="label">SES</span>
                <span className="mono" style={sesStyle(sesPct)}>
                  {sesPct}%
                </span>
              </>
            )}
            {wkPct !== null && (
              <>
                <span className="label">WK</span>
                <span className="mono" style={sesStyle(wkPct)}>
                  {wkPct}%
                </span>
              </>
            )}
            <span className="label">RESET</span>
            <span className="mono">{resetLabel}</span>
          </span>
        ) : (
          <span className="label">IDLE</span>
        )}

        <span className="label">UPLINK</span>
        <span className="pip pip--signal" aria-hidden />
        <span className="mono text-signal">{clock}</span>
      </div>
    </header>
  );
}
