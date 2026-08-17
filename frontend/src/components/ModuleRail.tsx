import { useEffect, useState } from "react";
import { usePolling } from "../api";
import { useStore, pipStatus } from "../store";

/**
 * Module sidebar. Module buttons fill the top; persistent per-provider
 * telemetry lanes are pinned to the bottom (marginTop: auto).
 * /api/session polled every 15 s; the RESET countdown ticks locally
 * every 1 s off the shared clock between polls.
 */

interface Lane {
  week_reset_at?: string;
  model?: string;
  ctx_limit?: number;
  ctx_pct?: number;
  used_pct?: number;
  week_pct?: number;
  reset_at?: string;
  active?: boolean;
}

interface SessionStats {
  lanes?: { zai?: Lane; gemini?: Lane; claude?: Lane };
}

function resetCountdown(iso: string, now: number): string | null {
  const ms = new Date(iso).getTime() - now;
  if (isNaN(ms) || ms <= 0) return null;
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h >= 48) return `${Math.floor(h / 24)}d ${h % 24}h`; // weekly-style windows
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// 1_000_000 -> "1M", 200_000 -> "200K" — labels the CTX gauge with its
// actual per-model denominator so a stale table entry is visible at a glance.
function formatCtxLimit(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 1_000_000)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return `${n}`;
}

export default function ModuleRail() {
  const machine = useStore((s) => s.config?.machine);
  const modules = useStore((s) => s.config?.modules) ?? [];
  const activeId = useStore((s) => s.activeModuleId);
  const setActive = useStore((s) => s.setActive);
  const healthMap = useStore((s) => s.healthMap);
  const starting = useStore((s) => s.starting);

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const { data: stats } = usePolling<SessionStats>("/api/session", 15_000);

  return (
    <nav className="hud hud--bracket reveal reveal-2 m-2 flex w-44 shrink-0 flex-col gap-1 p-2">
      <div className="mb-1 border-b border-edge px-1 pb-2">
        <div className="panel-title">▸ RAILJACK</div>
        <div className="label">// {(machine ?? "—").toUpperCase()}</div>
      </div>
      <span className="label mb-1 px-1">MODULES</span>
      {modules.map((m) => {
        const active = m.id === activeId;
        const status = m.health
          ? pipStatus(healthMap[m.id], starting[m.id], Date.now())
          : "auto";
        const pip =
          status === "auto"
            ? "pip pip--on"
            : status === "ok"
              ? "pip pip--go"
              : status === "starting"
                ? "pip pip--hazard"
                : status === "down"
                  ? "pip pip--crit"
                  : "pip";
        return (
          <button
            key={m.id}
            onClick={() => setActive(m.id)}
            className={`btn hud--bracket relative flex items-center gap-2 ${active ? "btn--signal" : ""}`}
          >
            <span className={pip} aria-hidden />
            <span className="module-label text-left">{m.title}</span>
          </button>
        );
      })}

      {/* Telemetry strip — pinned bottom, persistent per-provider lanes. Each
          lane always shows model + ctx capacity + quota used-% + reset (quota
          data survives idle); the green/red pip is the only idle-dependent
          element (green = a session for that provider wrote within
          ACTIVE_WINDOW ~90s). marginTop inline (not mt-auto) — global.css's
          unlayered `* { margin: 0 }` reset outranks Tailwind's layered
          utilities; inline style wins. */}
      <div
        className="flex flex-col gap-1.5 border-t border-edge"
        style={{ marginTop: "auto", paddingTop: "0.5rem" }}
      >
        <TelemetryLane label="GOOGLE" lane={stats?.lanes?.gemini} now={now} />
        <TelemetryLane label="CLAUDE / GPT" lane={stats?.lanes?.claude} now={now} />
        <TelemetryLane label="Z.AI" lane={stats?.lanes?.zai} now={now} />
      </div>
    </nav>
  );
}

/** Percentage metric — value glows in its own color; flips hazard ≥70%,
 * critical ≥90% (Railjack telemetry grammar). */
function Metric({ label, value }: { label: string; value: number }) {
  const color =
    value >= 90
      ? "var(--color-critical, var(--error-deep))"
      : value >= 70
        ? "var(--color-hazard, var(--warning))"
        : "var(--color-phosphor, var(--ink))";
  return <MetricRaw label={label} value={`${value}%`} color={color} />;
}

function MetricRaw({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex items-center justify-between gap-1">
      <span className="label">{label}</span>
      <span className="pct" style={{ color: color ?? "var(--color-phosphor, var(--ink))" }}>
        {value}
      </span>
    </div>
  );
}

/** One provider lane — always shows model + ctx capacity + quota used-% + reset
 * (quota data survives idle); the green/red pip is the only idle-dependent
 * element. ctx_pct (live context-window fill) shows only when the provider is
 * active. Persistent — never collapses to IDLE. */
function TelemetryLane({
  label,
  lane,
  now,
}: {
  label: string;
  lane?: Lane;
  now: number;
}) {
  const active = lane?.active ?? false;
  const reset = lane?.reset_at ? resetCountdown(lane.reset_at, now) : null;
  const wkReset = lane?.week_reset_at ? resetCountdown(lane.week_reset_at, now) : null;
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-2">
        <span className={active ? "pip pip--go" : "pip pip--crit"} />
        <span className="label truncate">{lane?.model ?? label}</span>
        {lane?.ctx_limit ? (
          <span className="label" style={{ opacity: 0.6 }}>
            ({formatCtxLimit(lane.ctx_limit)})
          </span>
        ) : null}
      </div>
      {lane?.used_pct !== undefined && <Metric label="SES" value={lane.used_pct} />}
      {active && lane?.ctx_pct !== undefined && <Metric label="CTX" value={lane.ctx_pct} />}
      {lane?.week_pct !== undefined && <Metric label="WK" value={lane.week_pct} />}
      {reset && <MetricRaw label="RESET" value={reset} />}
      {wkReset && <MetricRaw label="WK RESET" value={wkReset} />}
    </div>
  );
}
