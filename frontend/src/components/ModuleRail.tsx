import { useEffect, useState, type CSSProperties } from "react";
import { usePolling } from "../api";
import { useStore, pipStatus } from "../store";

/**
 * Module sidebar. Module buttons fill the top; the session telemetry strip is
 * pinned to the bottom (mt-auto). /api/session polled every 15 s; the RESET
 * countdown ticks locally every 1 s off the shared clock between polls.
 */

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

// 1_000_000 -> "1M", 200_000 -> "200K" — labels the CTX gauge with its
// actual per-model denominator so a stale table entry is visible at a glance.
function formatCtxLimit(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 1_000_000)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return `${n}`;
}

function formatModelName(model: string, provider: string): string {
  const m = model.toLowerCase();
  if (m.includes("gemini")) return "GEMINI 3.6";
  if (m.includes("sonnet") || m.includes("3-5")) return "CLAUDE 3.5";
  if (m.includes("haiku")) return "CLAUDE HAIKU";
  if (m.includes("cco")) return "CCO GLM-5.2";
  if (m.includes("glm")) return "GLM-5.2";
  return (model || provider || "MODEL").toUpperCase();
}

function formatResetTime(seconds: number): string {
  if (seconds <= 0) return "READY";
  if (seconds >= 86400) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${days}d ${hours}h ${mins}m`;
  }
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return `${hours}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export default function ModuleRail() {
  const machine = useStore((s) => s.config?.machine);
  const modules = useStore((s) => s.config?.modules) ?? [];
  const activeId = useStore((s) => s.activeModuleId);
  const setActive = useStore((s) => s.setActive);
  const healthMap = useStore((s) => s.healthMap);
  const starting = useStore((s) => s.starting);

  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  const { data: session } = usePolling<Session>("/api/session", 15_000);

  // Telemetry thresholds. ctx pip goes crit ≥90 %, hazard ≥70 %.
  const pct = session?.context_pct ?? 0;
  const pipClass =
    pct >= 90 ? "pip pip--crit" : pct >= 70 ? "pip pip--hazard" : "pip pip--go";
  const sesPct = session?.session_pct ?? null;
  const wkPct = session?.weekly_pct ?? null;
  const sesStyle = (p: number): CSSProperties | undefined =>
    p >= 90
      ? { color: "var(--color-critical)" }
      : p >= 70
        ? { color: "var(--color-hazard)" }
        : undefined;

  const isApi = session?.source === "api";
  const resetMs = isApi && session?.reset_at ? Date.parse(session.reset_at) : 0;
  const remaining = resetMs ? Math.max(0, resetMs - now.getTime()) : 0;
  const totalSec = Math.floor(remaining / 1000);
  const resetLabel = formatResetTime(totalSec);

  const provider = session?.provider ?? "";
  const modelName = session ? formatModelName(session.model, session.provider) : "";

  // Quota Label based on provider
  const quotaLabel =
    provider === "gemini"
      ? "7D BURN"
      : provider === "cco"
        ? "$ SPEND"
        : provider === "zai"
          ? "CODING"
          : "SES";
  const quotaValue = provider === "gemini" ? (wkPct ?? sesPct) : sesPct;

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

      {/* Telemetry pinned to the bottom of the rail. Provider name is the
          header; each metric is a label-left / value-right row. */}
      <div className="mt-auto flex flex-col gap-1 border-t border-edge pt-2">
        {session && !session.idle ? (
          <>
            <div className="flex items-center gap-2 px-1">
              <span className={pipClass} aria-hidden />
              <span className="label font-bold text-signal">{modelName}</span>
            </div>

            <div className="flex items-center justify-between px-1">
              <span className="label">
                CTX{session.context_limit ? `(${formatCtxLimit(session.context_limit)})` : ""}
              </span>
              <span className="pct">{session.context_pct}%</span>
            </div>

            {quotaValue !== null && (
              <div className="flex items-center justify-between px-1">
                <span className="label">{quotaLabel}</span>
                <span className="pct" style={sesStyle(quotaValue)}>
                  {quotaValue}%
                </span>
              </div>
            )}

            {resetMs > 0 && (
              <div className="flex items-center justify-between px-1">
                <span className="label">RESET</span>
                <span className="pct">{resetLabel}</span>
              </div>
            )}
          </>
        ) : (
          <span className="label px-1">IDLE</span>
        )}
      </div>
    </nav>
  );
}
