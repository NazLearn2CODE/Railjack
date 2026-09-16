import { useState, useEffect, useCallback } from "react";
import type { FC } from "react";
import {
  FolderGit2,
  RotateCw,
  AlertTriangle,
  CheckCircle2,
  Circle,
  Shield,
  GitCommit,
  Clock,
  Sparkles,
  Layers,
  Compass,
} from "lucide-react";
import { fetchJSON } from "../api";
import type { ModuleConfig } from "../store";

// ── Types ───────────────────────────────────────────────────────────────────

export interface NextAction {
  text: string;
  source_file: string;
  quote: string;
}

export interface LastCommit {
  hash: string;
  date: string;
  relative_date: string;
  author: string;
  message: string;
}

export interface ProtocolStatus {
  index: boolean;
  codecompass_state: "filled" | "template-unfilled" | "missing";
  gates: boolean;
  decisions_n: number;
  sessions_n: number;
}

export interface MilestoneItem {
  checked: boolean;
  name: string;
  phase: string;
  date: string;
  commit: string;
}

export interface GateItem {
  name: string;
  cmd: string;
}

export interface DecisionItem {
  date: string;
  title: string;
  status: string;
  filename: string;
}

export interface SessionLogItem {
  date: string;
  title: string;
  filename: string;
  snippet: string;
}

export interface ProjectSummary {
  id: string;
  name: string;
  path: string;
  phase: string;
  health: "green" | "amber" | "red" | "grey";
  trend: string;
  trend_sparkline: number[];
  pct: number | null;
  pct_basis: string;
  next_action: NextAction;
  blockers: string[];
  last_commit: LastCommit | null;
  protocol: ProtocolStatus;
}

export interface ProjectDetail extends ProjectSummary {
  repo_type: "game" | "tool";
  milestones: MilestoneItem[];
  gates: GateItem[];
  decisions: DecisionItem[];
  session_logs: SessionLogItem[];
  git_log: LastCommit[];
}

const GAME_PHASES = [
  "concept",
  "pre-production",
  "production",
  "alpha",
  "beta",
  "gold",
  "live-ops",
];

const TOOL_PHASES = [
  "bootstrap",
  "brief/planning",
  "build",
  "harden/gates",
  "validated/shipped",
  "live-ops/maintenance",
];

// ── Helpers ─────────────────────────────────────────────────────────────────

function getHealthPipClass(health: string) {
  switch (health) {
    case "green":
      return "pip pip--go";
    case "amber":
      return "pip pip--hazard";
    case "red":
      return "pip pip--crit";
    default:
      return "pip opacity-40";
  }
}

function getHealthBadge(health: string) {
  switch (health) {
    case "green":
      return (
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-xs border border-emerald-500/30 text-emerald-400 bg-emerald-950/20">
          HEALTH: GREEN
        </span>
      );
    case "amber":
      return (
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-xs border border-amber-500/30 text-amber-400 bg-amber-950/20">
          HEALTH: AMBER
        </span>
      );
    case "red":
      return (
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-xs border border-rose-500/30 text-rose-400 bg-rose-950/20">
          HEALTH: RED
        </span>
      );
    default:
      return (
        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded-xs border border-neutral-700 text-neutral-400 bg-neutral-900/30">
          UNTRACKED
        </span>
      );
  }
}

// ── Component ───────────────────────────────────────────────────────────────

const ProjectsPanel: FC<{ module: ModuleConfig }> = () => {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [detailLoading, setDetailLoading] = useState<boolean>(false);
  const [rescanning, setRescanning] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"milestones" | "gates" | "decisions" | "sessions" | "git">("milestones");

  // Load summary
  const loadProjects = useCallback(async () => {
    try {
      setError(null);
      const data = await fetchJSON<ProjectSummary[]>("/api/projects/summary");
      setProjects(data);
      if (data.length > 0) {
        setSelectedId((curr) => (curr && data.some((p) => p.id === curr) ? curr : data[0].id));
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  // Load selected project detail
  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let alive = true;
    setDetailLoading(true);
    fetchJSON<ProjectDetail>(`/api/projects/${selectedId}`)
      .then((d) => {
        if (alive) setDetail(d);
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selectedId]);

  // Rescan action
  const handleRescan = async () => {
    setRescanning(true);
    try {
      const res = await fetchJSON<{ status: string; count: number; projects: ProjectSummary[] }>(
        "/api/projects/rescan",
        { method: "POST" }
      );
      if (res.projects) {
        setProjects(res.projects);
        if (res.projects.length > 0) {
          const validSelected = selectedId && res.projects.some((p) => p.id === selectedId);
          const newId = validSelected ? selectedId : res.projects[0].id;
          setSelectedId(newId);
          // Refetch detail
          const freshDetail = await fetchJSON<ProjectDetail>(`/api/projects/${newId}`);
          setDetail(freshDetail);
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRescanning(false);
    }
  };

  const selectedProject = projects.find((p) => p.id === selectedId) || null;

  // Pipeline phases
  const pipelinePhases = detail?.repo_type === "game" ? GAME_PHASES : TOOL_PHASES;
  const currentPhaseIndex = pipelinePhases.findIndex(
    (ph) => ph.toLowerCase() === (detail?.phase || selectedProject?.phase || "").toLowerCase()
  );

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-[var(--color-vacuum)] text-[var(--color-phosphor)]">
      {/* ── Top Header & Controls ── */}
      <div className="hud hud--bracket flex shrink-0 items-center justify-between border-b border-edge px-4 py-2.5 bg-[var(--color-panel)]">
        <div className="flex items-center gap-3">
          <FolderGit2 className="h-5 w-5 text-[var(--color-signal)]" />
          <h1 className="font-display font-semibold tracking-wider text-base text-[var(--color-phosphor)]">
            PROJECTS
          </h1>
          <span className="text-xs font-mono text-[var(--color-muted)] bg-[var(--color-panel-2)] px-2 py-0.5 rounded-xs border border-edge">
            {projects.length} TRACKED
          </span>
          <span className="text-[11px] font-mono text-[var(--color-muted)] hidden sm:inline">
            ROOT: ~/GameDev
          </span>
        </div>

        <div className="flex items-center gap-2">
          {error && (
            <span className="text-xs text-rose-400 font-mono mr-2 flex items-center gap-1">
              <AlertTriangle className="h-3.5 w-3.5" />
              {error}
            </span>
          )}
          <button
            onClick={handleRescan}
            disabled={rescanning || loading}
            className="btn btn--compact flex items-center gap-1.5 text-xs font-mono"
            title="Force re-scan all projects bypassing cache"
          >
            <RotateCw className={`h-3.5 w-3.5 ${rescanning ? "animate-spin text-[var(--color-signal)]" : ""}`} />
            {rescanning ? "RESCANNING…" : "↻ RESCAN"}
          </button>
        </div>
      </div>

      {/* ── Scrollable Body ── */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-4 gap-4">
        {loading ? (
          <div className="flex h-40 items-center justify-center text-sm font-mono text-[var(--color-muted)]">
            Scanning projects directory…
          </div>
        ) : (
          <>
            {/* ── LEVEL 1: Projects Card Grid ── */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-mono text-[var(--color-muted)] uppercase tracking-wider">
                  Portfolio Status Scan (RAG + Trend + Phase)
                </span>
                <span className="text-[11px] font-mono text-[var(--color-muted)]">
                  Click a card to inspect telemetry & next action
                </span>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
                {projects.map((p) => {
                  const isSelected = p.id === selectedId;
                  return (
                    <div
                      key={p.id}
                      onClick={() => setSelectedId(p.id)}
                      className={`hud hud--bracket p-3 cursor-pointer transition-all flex flex-col justify-between select-none ${
                        isSelected
                          ? "border-[var(--color-signal)] bg-[var(--color-panel-2)] shadow-[0_0_12px_rgba(56,224,255,0.15)]"
                          : "hover:border-[var(--color-phosphor-dim)] bg-[var(--color-panel)]"
                      }`}
                    >
                      {/* Card Header: Pip, Name, Health */}
                      <div>
                        <div className="flex items-center justify-between gap-2 mb-1.5">
                          <div className="flex items-center gap-2 min-w-0">
                            <span className={getHealthPipClass(p.health)} />
                            <span className="font-display font-semibold text-sm text-[var(--color-phosphor)] truncate">
                              {p.name}
                            </span>
                          </div>
                          {getHealthBadge(p.health)}
                        </div>

                        {/* Phase & Pct Row */}
                        <div className="flex items-center justify-between text-xs font-mono my-2 border-y border-edge/50 py-1">
                          <span className="text-[var(--color-signal)] uppercase tracking-wider text-[11px] font-semibold">
                            {p.phase}
                          </span>
                          <span className="text-[var(--color-phosphor-dim)] text-[11px]">
                            {p.pct !== null ? `${p.pct}%` : "—"}
                            <span className="text-[10px] text-[var(--color-muted)] ml-1">
                              ({p.pct_basis})
                            </span>
                          </span>
                        </div>

                        {/* Sparkline & Trend */}
                        <div className="flex items-center justify-between gap-2 my-2">
                          <div className="flex items-end gap-0.5 h-6 py-0.5 bg-black/40 px-1 rounded-xs border border-edge/40 flex-1">
                            {p.trend_sparkline.map((count, idx) => {
                              const maxVal = Math.max(1, ...p.trend_sparkline);
                              const heightPct = count === 0 ? 10 : Math.max(25, Math.round((count / maxVal) * 100));
                              return (
                                <div
                                  key={idx}
                                  className={`flex-1 rounded-xs transition-all ${
                                    count > 0 ? "bg-[var(--color-signal)]" : "bg-[var(--color-edge)] opacity-30"
                                  }`}
                                  style={{ height: `${heightPct}%` }}
                                  title={`Day ${idx + 1}: ${count} commits`}
                                />
                              );
                            })}
                          </div>
                          <span className="text-[10px] font-mono text-[var(--color-muted)] whitespace-nowrap">
                            {p.trend}
                          </span>
                        </div>

                        {/* Next Action Teaser */}
                        <div className="text-xs text-[var(--color-phosphor-dim)] line-clamp-2 mt-2 leading-relaxed bg-[var(--color-vacuum)]/60 p-1.5 rounded-xs border border-edge/40">
                          <span className="text-[var(--color-signal)] font-medium">▸ Next: </span>
                          <span className="text-neutral-300 font-mono text-[11px]">{p.next_action.text}</span>
                        </div>
                      </div>

                      {/* Card Footer: Blockers pill if present */}
                      {p.blockers.length > 0 && (
                        <div className="mt-2.5 flex items-center gap-1 text-[11px] font-mono text-amber-400 bg-amber-950/30 border border-amber-500/30 px-2 py-0.5 rounded-xs">
                          <AlertTriangle className="h-3 w-3 shrink-0" />
                          <span className="truncate">{p.blockers[0]}</span>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            {/* ── LEVEL 2: Focused Project Detail & Telemetry ── */}
            {selectedProject && (
              <div className="flex flex-col gap-4 mt-2">
                {/* 1. Status Header */}
                <div className="hud hud--bracket p-4 bg-[var(--color-panel)]">
                  <div className="flex flex-wrap items-center justify-between gap-4">
                    <div>
                      <div className="flex items-center gap-3">
                        <span className={getHealthPipClass(selectedProject.health)} />
                        <h2 className="font-display text-xl font-bold text-[var(--color-phosphor)] tracking-wide">
                          {detail?.name || selectedProject.name}
                        </h2>
                        {getHealthBadge(selectedProject.health)}
                        <span className="text-xs font-mono text-[var(--color-signal)] bg-[var(--color-signal-deep)]/40 px-2 py-0.5 rounded-xs border border-[var(--color-signal)]/40 uppercase">
                          {detail?.repo_type || "GAME"}
                        </span>
                        <span className="text-xs font-mono text-neutral-400 bg-black/40 px-2 py-0.5 rounded-xs border border-edge">
                          PHASE: <strong className="text-[var(--color-phosphor)] uppercase">{selectedProject.phase}</strong>
                        </span>
                        <span className="text-xs font-mono text-neutral-400">
                          PROGRESS:{" "}
                          <strong className="text-[var(--color-phosphor)]">
                            {selectedProject.pct !== null ? `${selectedProject.pct}%` : "—"}
                          </strong>{" "}
                          <span className="text-neutral-500">({selectedProject.pct_basis})</span>
                        </span>
                      </div>
                      <p className="text-xs font-mono text-[var(--color-muted)] mt-1.5 flex items-center gap-2">
                        <span>{selectedProject.path}</span>
                        <span>·</span>
                        <span>
                          Protocol:{" "}
                          {selectedProject.protocol.index ? (
                            <span className="text-emerald-400 font-semibold">index.md ✓</span>
                          ) : (
                            <span className="text-neutral-500">no index</span>
                          )}
                          {" · "}
                          {selectedProject.protocol.gates ? (
                            <span className="text-emerald-400 font-semibold">gates.yml ✓</span>
                          ) : (
                            <span className="text-neutral-500">no gates</span>
                          )}
                          {" · "}
                          CodeCompass:{" "}
                          <span
                            className={
                              selectedProject.protocol.codecompass_state === "filled"
                                ? "text-emerald-400"
                                : selectedProject.protocol.codecompass_state === "template-unfilled"
                                ? "text-amber-400"
                                : "text-neutral-500"
                            }
                          >
                            {selectedProject.protocol.codecompass_state}
                          </span>
                        </span>
                      </p>
                    </div>

                    {/* Last commit chip */}
                    {selectedProject.last_commit && (
                      <div className="flex items-center gap-3 bg-[var(--color-vacuum)] px-3 py-2 rounded-xs border border-edge text-xs font-mono">
                        <GitCommit className="h-4 w-4 text-[var(--color-signal)] shrink-0" />
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-[var(--color-signal)] font-semibold">
                              {selectedProject.last_commit.hash}
                            </span>
                            <span className="text-neutral-400">{selectedProject.last_commit.relative_date}</span>
                            <span className="text-neutral-500">by {selectedProject.last_commit.author}</span>
                          </div>
                          <p className="text-neutral-300 truncate max-w-sm">
                            {selectedProject.last_commit.message}
                          </p>
                        </div>
                      </div>
                    )}
                  </div>
                </div>

                {/* 2. Phase Bar (Segmented Pipeline Bar) */}
                <div className="hud hud--bracket p-4 bg-[var(--color-panel)]">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-mono text-[var(--color-muted)] uppercase tracking-wider flex items-center gap-1.5">
                      <Layers className="h-3.5 w-3.5 text-[var(--color-signal)]" />
                      Lifecycle Phase Pipeline ({detail?.repo_type === "game" ? "Game Pipeline" : "ADW Pipeline"})
                    </span>
                    <span className="text-xs font-mono text-[var(--color-signal)] font-semibold uppercase">
                      Current: {selectedProject.phase}
                    </span>
                  </div>

                  {selectedProject.phase === "no-protocol" ? (
                    <div className="p-3 bg-neutral-900/60 border border-neutral-700 rounded-xs flex items-center justify-between text-xs font-mono">
                      <span className="text-neutral-400">
                        ⚠ UNTRACKED PROJECT — No A-project/ protocol structure found.
                      </span>
                      <span className="text-[var(--color-signal)]">
                        Action: Stamp via <code>project-bootstrap</code> skill
                      </span>
                    </div>
                  ) : (
                    <div className="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-7 gap-1.5 text-xs font-mono">
                      {pipelinePhases.map((phaseName, idx) => {
                        const isPast = currentPhaseIndex > -1 && idx < currentPhaseIndex;
                        const isCurrent = currentPhaseIndex === idx;
                        const isFuture = currentPhaseIndex > -1 && idx > currentPhaseIndex;

                        return (
                          <div
                            key={phaseName}
                            className={`flex flex-col items-center justify-center p-2 rounded-xs border text-center transition-all ${
                              isCurrent
                                ? "border-[var(--color-signal)] bg-[var(--color-signal-deep)]/40 text-[var(--color-signal)] font-bold shadow-[0_0_8px_rgba(56,224,255,0.3)]"
                                : isPast
                                ? "border-emerald-500/40 bg-emerald-950/20 text-emerald-400"
                                : isFuture
                                ? "border-edge bg-black/20 text-[var(--color-faint)]"
                                : "border-edge text-[var(--color-muted)]"
                            }`}
                          >
                            <div className="flex items-center gap-1 mb-0.5">
                              {isPast ? (
                                <CheckCircle2 className="h-3 w-3 text-emerald-400" />
                              ) : isCurrent ? (
                                <span className="pip pip--signal inline-block" />
                              ) : (
                                <Circle className="h-3 w-3 text-[var(--color-faint)]" />
                              )}
                              <span className="text-[10px] uppercase font-semibold">Step {idx + 1}</span>
                            </div>
                            <span className="truncate w-full uppercase">{phaseName}</span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>

                {/* 3. GTD Next-Action Card (High Visibility) */}
                <div className="hud hud--bracket p-4 bg-gradient-to-r from-[var(--color-panel-2)] to-[var(--color-panel)] border-[var(--color-signal)]/50">
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <Sparkles className="h-4 w-4 text-[var(--color-signal)]" />
                      <span className="text-xs font-display font-semibold uppercase tracking-wider text-[var(--color-signal)]">
                        NEXT ACTION (Sourced from GTD Priority Ladder)
                      </span>
                    </div>
                    <span className="text-xs font-mono text-[var(--color-muted)] bg-black/50 px-2 py-0.5 rounded-xs border border-edge">
                      Source: {selectedProject.next_action.source_file}
                    </span>
                  </div>

                  <div className="my-2">
                    <h3 className="text-base sm:text-lg font-mono font-bold text-[var(--color-phosphor)]">
                      {selectedProject.next_action.text}
                    </h3>
                  </div>

                  <div className="mt-2 bg-black/60 p-2.5 rounded-xs border border-edge text-xs font-mono text-neutral-400 italic">
                    <span className="text-[var(--color-muted)] not-italic font-semibold mr-1">Quote:</span>
                    &ldquo;{selectedProject.next_action.quote}&rdquo;
                  </div>
                </div>

                {/* 4. Blockers Card (if any) */}
                {selectedProject.blockers.length > 0 && (
                  <div className="hud hud--bracket p-4 bg-amber-950/20 border-amber-500/40">
                    <div className="flex items-center gap-2 text-amber-400 font-display font-semibold text-xs tracking-wider mb-2">
                      <AlertTriangle className="h-4 w-4" />
                      ACTIVE BLOCKERS & HANDOFF NOTES ({selectedProject.blockers.length})
                    </div>
                    <ul className="space-y-1.5 text-xs font-mono text-amber-200">
                      {selectedProject.blockers.map((b, idx) => (
                        <li key={idx} className="flex items-start gap-2">
                          <span className="text-amber-500 font-bold">▸</span>
                          <span>{b}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* 5. Detail Subsections (Tabs) */}
                <div className="hud hud--bracket bg-[var(--color-panel)]">
                  {/* Tabs bar */}
                  <div className="flex items-center gap-1 border-b border-edge px-3 py-1.5 overflow-x-auto">
                    <button
                      onClick={() => setActiveTab("milestones")}
                      className={`btn btn--compact text-xs font-mono flex items-center gap-1.5 ${
                        activeTab === "milestones" ? "btn--primary text-[var(--color-signal)]" : ""
                      }`}
                    >
                      <CheckCircle2 className="h-3.5 w-3.5" />
                      Milestones ({detail?.milestones.length || 0})
                    </button>
                    <button
                      onClick={() => setActiveTab("gates")}
                      className={`btn btn--compact text-xs font-mono flex items-center gap-1.5 ${
                        activeTab === "gates" ? "btn--primary text-[var(--color-signal)]" : ""
                      }`}
                    >
                      <Shield className="h-3.5 w-3.5" />
                      Gates ({detail?.gates.length || 0})
                    </button>
                    <button
                      onClick={() => setActiveTab("decisions")}
                      className={`btn btn--compact text-xs font-mono flex items-center gap-1.5 ${
                        activeTab === "decisions" ? "btn--primary text-[var(--color-signal)]" : ""
                      }`}
                    >
                      <Compass className="h-3.5 w-3.5" />
                      Decisions ({detail?.decisions.length || 0})
                    </button>
                    <button
                      onClick={() => setActiveTab("sessions")}
                      className={`btn btn--compact text-xs font-mono flex items-center gap-1.5 ${
                        activeTab === "sessions" ? "btn--primary text-[var(--color-signal)]" : ""
                      }`}
                    >
                      <Clock className="h-3.5 w-3.5" />
                      Session Logs ({detail?.session_logs.length || 0})
                    </button>
                    <button
                      onClick={() => setActiveTab("git")}
                      className={`btn btn--compact text-xs font-mono flex items-center gap-1.5 ${
                        activeTab === "git" ? "btn--primary text-[var(--color-signal)]" : ""
                      }`}
                    >
                      <GitCommit className="h-3.5 w-3.5" />
                      Git History ({detail?.git_log.length || 0})
                    </button>
                  </div>

                  {/* Tab Contents */}
                  <div className="p-4">
                    {detailLoading ? (
                      <div className="py-8 text-center text-xs font-mono text-[var(--color-muted)]">
                        Loading detailed telemetry…
                      </div>
                    ) : (
                      <>
                        {/* Tab: Milestones */}
                        {activeTab === "milestones" && (
                          <div>
                            {detail?.milestones && detail.milestones.length > 0 ? (
                              <div className="overflow-x-auto">
                                <table className="w-full text-left text-xs font-mono border-collapse">
                                  <thead>
                                    <tr className="border-b border-edge text-[var(--color-muted)] uppercase">
                                      <th className="py-2 px-3 w-10">Status</th>
                                      <th className="py-2 px-3">Milestone Scope</th>
                                      <th className="py-2 px-3">Phase</th>
                                      <th className="py-2 px-3">Date</th>
                                      <th className="py-2 px-3">Commit</th>
                                    </tr>
                                  </thead>
                                  <tbody className="divide-y divide-edge/40">
                                    {detail.milestones.map((m, idx) => (
                                      <tr
                                        key={idx}
                                        className={
                                          m.checked
                                            ? "text-neutral-400 bg-black/20"
                                            : "text-[var(--color-phosphor)] font-medium"
                                        }
                                      >
                                        <td className="py-2.5 px-3">
                                          {m.checked ? (
                                            <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                                          ) : (
                                            <Circle className="h-4 w-4 text-neutral-500" />
                                          )}
                                        </td>
                                        <td className="py-2.5 px-3">{m.name}</td>
                                        <td className="py-2.5 px-3 text-[var(--color-signal)] uppercase">
                                          {m.phase || "—"}
                                        </td>
                                        <td className="py-2.5 px-3 text-neutral-400">{m.date || "—"}</td>
                                        <td className="py-2.5 px-3 text-neutral-400">
                                          {m.commit ? <code>{m.commit}</code> : "—"}
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            ) : (
                              <p className="text-xs font-mono text-neutral-500 py-4">
                                No milestones table found in <code>A-project/index.md</code>.
                              </p>
                            )}
                          </div>
                        )}

                        {/* Tab: Gates */}
                        {activeTab === "gates" && (
                          <div className="space-y-3">
                            {detail?.gates && detail.gates.length > 0 ? (
                              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                {detail.gates.map((g, idx) => (
                                  <div
                                    key={idx}
                                    className="p-3 bg-black/40 border border-edge rounded-xs flex flex-col justify-between"
                                  >
                                    <div className="flex items-center justify-between mb-1.5">
                                      <span className="font-display font-semibold text-xs text-[var(--color-signal)] uppercase">
                                        {g.name}
                                      </span>
                                      <Shield className="h-3.5 w-3.5 text-emerald-400" />
                                    </div>
                                    <pre className="text-[11px] font-mono bg-black/60 p-2 rounded-xs text-neutral-300 overflow-x-auto">
                                      {g.cmd}
                                    </pre>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <div className="p-4 bg-neutral-900/40 border border-edge rounded-xs text-xs font-mono text-neutral-400">
                                ⚠ No <code>gates.yml</code> in project root. Defining machine-checkable verification
                                gates (lint, test, build) provides the executable definition of &quot;green&quot;.
                              </div>
                            )}
                          </div>
                        )}

                        {/* Tab: Decisions */}
                        {activeTab === "decisions" && (
                          <div className="space-y-2">
                            {detail?.decisions && detail.decisions.length > 0 ? (
                              detail.decisions.map((d, idx) => (
                                <div
                                  key={idx}
                                  className="p-3 bg-black/40 border border-edge rounded-xs flex items-center justify-between text-xs font-mono"
                                >
                                  <div className="flex items-center gap-3">
                                    <span className="text-[var(--color-muted)]">{d.date || "—"}</span>
                                    <span className="text-[var(--color-phosphor)] font-medium">{d.title}</span>
                                  </div>
                                  <div className="flex items-center gap-2">
                                    <span
                                      className={`text-[10px] uppercase px-1.5 py-0.5 rounded-xs border ${
                                        d.status === "accepted"
                                          ? "border-emerald-500/30 text-emerald-400 bg-emerald-950/20"
                                          : d.status === "deprecated"
                                          ? "border-neutral-700 text-neutral-500"
                                          : "border-amber-500/30 text-amber-400"
                                      }`}
                                    >
                                      {d.status}
                                    </span>
                                    <span className="text-neutral-500 text-[11px]">{d.filename}</span>
                                  </div>
                                </div>
                              ))
                            ) : (
                              <p className="text-xs font-mono text-neutral-500 py-4">
                                No ADR decisions recorded in <code>A-project/decisions/</code>.
                              </p>
                            )}
                          </div>
                        )}

                        {/* Tab: Sessions */}
                        {activeTab === "sessions" && (
                          <div className="space-y-2">
                            {detail?.session_logs && detail.session_logs.length > 0 ? (
                              detail.session_logs.map((s, idx) => (
                                <div
                                  key={idx}
                                  className="p-3 bg-black/40 border border-edge rounded-xs text-xs font-mono"
                                >
                                  <div className="flex items-center justify-between mb-1">
                                    <div className="flex items-center gap-2">
                                      <Clock className="h-3.5 w-3.5 text-[var(--color-signal)]" />
                                      <span className="text-[var(--color-phosphor)] font-semibold">
                                        {s.title || s.filename}
                                      </span>
                                    </div>
                                    <span className="text-[var(--color-muted)]">{s.date || "—"}</span>
                                  </div>
                                  {s.snippet && (
                                    <p className="text-neutral-400 text-[11px] line-clamp-2 mt-1 pl-5">
                                      {s.snippet}
                                    </p>
                                  )}
                                </div>
                              ))
                            ) : (
                              <p className="text-xs font-mono text-neutral-500 py-4">
                                No session logs found in <code>B-sessions/</code>.
                              </p>
                            )}
                          </div>
                        )}

                        {/* Tab: Git Log */}
                        {activeTab === "git" && (
                          <div className="space-y-1.5">
                            {detail?.git_log && detail.git_log.length > 0 ? (
                              detail.git_log.map((c, idx) => (
                                <div
                                  key={idx}
                                  className="p-2.5 bg-black/40 border border-edge rounded-xs flex items-center justify-between text-xs font-mono"
                                >
                                  <div className="flex items-center gap-3 min-w-0">
                                    <code className="text-[var(--color-signal)] font-bold shrink-0">
                                      {c.hash}
                                    </code>
                                    <span className="text-neutral-300 truncate">{c.message}</span>
                                  </div>
                                  <div className="flex items-center gap-2 shrink-0 text-neutral-500 text-[11px] ml-4">
                                    <span>{c.relative_date}</span>
                                    <span>·</span>
                                    <span>{c.author}</span>
                                  </div>
                                </div>
                              ))
                            ) : (
                              <p className="text-xs font-mono text-neutral-500 py-4">
                                No git commits recorded for this project.
                              </p>
                            )}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default ProjectsPanel;
