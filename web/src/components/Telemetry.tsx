import { useState, useEffect, type ReactNode } from "react";
import { useStore } from "../store";
import { cn, connMeta, activeSkill } from "../util";
import { setWorkspaceRoot } from "../api";
import type { LogEntry } from "../store";

const CEPHALON_CHECKS = [
  ["CLAUDE.md", "claude_md"],
  ["CodeCompass.md", "code_compass"],
  ["A-project/index.md", "project_index"],
  ["Obsidian MCP", "obsidian_mcp"],
] as const;

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border border-edge bg-void/60">
      <div className="border-b border-edge-soft px-2.5 py-1.5">
        <span className="panel-title">{title}</span>
      </div>
      <div className="px-2.5 py-2">{children}</div>
    </div>
  );
}

function Readout({ label, value, muted, interactive, title }: { label: string; value: string; muted?: boolean; interactive?: ReactNode; title?: string }) {
  return (
    <div className="flex items-center justify-between border-b border-edge-soft py-1 last:border-0" title={title ?? (interactive ? undefined : value)}>
      <span className="label !text-[10px]">{label}</span>
      {interactive ? interactive : (
        <span
          className={cn("mono truncate text-[9px]", muted ? "text-faint" : "text-phosphor-dim")}
          style={{ maxWidth: "55%" }}
        >
          {value}
        </span>
      )}
    </div>
  );
}

const toneColor: Record<LogEntry["tone"], string> = {
  signal: "var(--color-signal)",
  hazard: "var(--color-hazard)",
  go: "var(--color-go)",
  crit: "var(--color-critical)",
  muted: "var(--color-muted)",
};
const tonePip: Record<LogEntry["tone"], string> = {
  signal: "pip--signal",
  hazard: "pip--hazard",
  go: "pip--go",
  crit: "pip--crit",
  muted: "",
};

interface FsBrowserModalProps {
  initialPath: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}

function FsBrowserModal({ initialPath, onSelect, onCancel }: FsBrowserModalProps) {
  const [dirs, setDirs] = useState<{ name: string; path: string }[]>([]);
  const [manualPath, setManualPath] = useState(initialPath || "");
  const [error, setError] = useState("");

  const loadDirs = async (path: string) => {
    try {
      const res = await fetch(`/api/fs/list?path=${encodeURIComponent(path)}`);
      if (!res.ok) {
        throw new Error("Failed to read directory");
      }
      const data = await res.json();
      setManualPath(data.current);
      setDirs(data.dirs);
      setError("");
    } catch (e: any) {
      setError(e.message || "Error reading directory");
    }
  };

  useEffect(() => {
    void loadDirs(initialPath || ".");
  }, [initialPath]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-void/80 backdrop-blur-sm p-4">
      <div className="reveal hud hud--bracket bg-panel border border-edge w-full max-w-[440px] flex flex-col min-h-0 max-h-[380px]">
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-edge px-3 py-1.5 bg-void/40">
          <span className="display text-[10px] font-semibold tracking-wider text-signal uppercase">
            SELECT WORKSPACE ROOT
          </span>
          <button className="text-faint hover:text-signal transition-colors text-[10px]" onClick={onCancel}>
            ✕
          </button>
        </div>

        {/* Manual path input & navigate */}
        <div className="p-2.5 border-b border-edge bg-void/20 flex gap-2 items-center">
          <input
            className="input !py-1 text-[10px]"
            value={manualPath}
            onChange={(e) => setManualPath(e.target.value)}
            placeholder="Absolute folder path..."
            onKeyDown={(e) => {
              if (e.key === "Enter") void loadDirs(manualPath);
            }}
          />
          <button
            className="btn !px-2.5 !py-1 !text-[10px] shrink-0"
            onClick={() => void loadDirs(manualPath)}
          >
            GO
          </button>
        </div>

        {/* Directory browser list */}
        <div className="flex-1 overflow-y-auto p-2.5 min-h-[140px] max-h-[200px]">
          {error ? (
            <div className="text-critical text-[10px]">{error}</div>
          ) : (
            <div className="space-y-1">
              {dirs.map((d, i) => (
                <div
                  key={i}
                  onDoubleClick={() => void loadDirs(d.path)}
                  onClick={() => setManualPath(d.path)}
                  className={cn(
                    "mono flex items-center gap-2 cursor-pointer p-1 text-[10px] border border-transparent hover:border-edge-soft hover:bg-void/40 transition-all select-none",
                    manualPath === d.path && "border-signal bg-void/60 text-signal"
                  )}
                >
                  <span className="text-hazard">{d.name === ".." ? "⬏" : "📁"}</span>
                  <span className="truncate text-phosphor-dim">{d.name}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Actions footer */}
        <div className="p-2.5 border-t border-edge bg-void/40 flex justify-end gap-2 shrink-0">
          <button className="btn !px-2.5 !py-1 !text-[10px]" onClick={onCancel}>
            CANCEL
          </button>
          <button
            className="btn btn--signal !px-2.5 !py-1 !text-[10px]"
            onClick={() => onSelect(manualPath)}
          >
            SELECT WORKSPACE
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Telemetry() {
  const log = useStore((s) => s.log);
  const conn = useStore((s) => s.conn);
  const activeId = useStore((s) => s.activeId);
  const t = useStore((s) => (s.activeId ? s.transcripts[s.activeId] : undefined));
  const health = useStore((s) => s.health);
  const init = useStore((s) => s.init);
  const c = connMeta(conn);
  const sb = health?.sandbox;
  const mcp = health?.mcp_servers ?? [];

  // ROOT editing
  const [editingRoot, setEditingRoot] = useState(false);
  const [rootInput, setRootInput] = useState("");
  const [showPicker, setShowPicker] = useState(false);
  const rootDisplay = health?.workspace?.root
    ? health.workspace.root.split("/").slice(-3).join("/")
    : "—";
  const fullRoot = health?.workspace?.root ?? "";

  const applyRoot = async () => {
    const trimmed = rootInput.trim();
    if (!trimmed) { setEditingRoot(false); return; }
    try {
      await setWorkspaceRoot(trimmed);
      await init();
      setEditingRoot(false);
    } catch {
      // keep open for correction
    }
  };

  return (
    <aside className="reveal reveal-4 m-2 ml-1 flex min-h-0 flex-col gap-2.5 overflow-y-auto p-1">
      <Panel title="SESSION">
        <Readout label="ID" value={activeId ? activeId.slice(0, 13) + "…" : "—"} />
        <Readout label="STATUS" value={(t?.status ?? "—").toUpperCase()} />
        <Readout label="SKILL" value={activeSkill(t?.rows)} />
        <Readout label="TOKENS" value={String(t?.tokens ?? 0)} />
        <Readout label="ROWS" value={String(t?.rows.length ?? 0)} />
        <Readout label="WS" value={c.label} />
      </Panel>

      <Panel title="ACTIVITY LOG">
        <div className="max-h-[240px] overflow-y-auto">
          {log.length === 0 ? (
            <div className="label py-1 text-faint">NO EVENTS</div>
          ) : (
            log.map((e) => (
              <div
                key={e.id}
                className="tick mono flex items-center gap-2 border-b border-edge-soft py-1 text-[9px]"
                title={e.label}
              >
                <span className={cn("pip", tonePip[e.tone])} style={{ width: 5, height: 5 }} />
                <span style={{ color: toneColor[e.tone] }}>{e.label}</span>
              </div>
            ))
          )}
        </div>
      </Panel>

      <Panel title="HOST">
        <Readout
          label="SANDBOX"
          value={sb ? (sb.active ? sb.mechanism.toUpperCase() : "FAIL-OPEN") : "—"}
          muted={!sb?.active}
          title={sb
            ? `Sandbox isolates agent file access to the workspace directory.\nMechanism: ${sb.mechanism}${sb.active ? " (active)" : " (inactive)"}${sb.reason ? "\n" + sb.reason : ""}`
            : "No sandbox info available"}
          {...(sb && !sb.active ? {
            interactive: (
              <button
                className="mono text-[9px] text-hazard hover:text-signal transition-colors"
                title={`Sandbox inactive — ${sb.reason ?? "unknown reason"}. Click to re-check.`}
                onClick={() => void init()}
              >
                {sb.mechanism?.toUpperCase() ?? "FAIL-OPEN"} ⚠
              </button>
            ),
          } : {})}
        />
        <Readout
          label="MCP"
          value={mcp.length ? `${mcp.length} SERVER${mcp.length > 1 ? "S" : ""}` : "NONE LOADED"}
          muted={!mcp.length}
        />
        {mcp.map((s) => (
          <div
            key={s.name}
            className="mono flex items-center gap-2 border-b border-edge-soft py-1 text-[9px] last:border-0"
          >
            <span className="text-signal">▸</span>
            <span className="truncate text-phosphor-dim" title={s.name}>{s.name}</span>
            <span className="ml-auto uppercase text-faint">{s.type}</span>
          </div>
        ))}
      </Panel>

      <Panel title="PROJECT">
        <Readout
          label="ROOT"
          value={rootDisplay}
          interactive={
            <div className="flex items-center gap-1.5 min-w-0" style={{ maxWidth: "70%" }}>
              {editingRoot ? (
                <input
                  autoFocus
                  className="input !py-0.5 !text-[9px] !px-1 min-w-0 flex-1"
                  placeholder="/path/to/workspace"
                  value={rootInput}
                  onChange={(e) => setRootInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void applyRoot();
                    if (e.key === "Escape") setEditingRoot(false);
                  }}
                  onBlur={() => void applyRoot()}
                />
              ) : (
                <>
                  <span
                    className="mono truncate text-[9px] text-phosphor-dim hover:text-signal transition-colors cursor-pointer text-right min-w-0 flex-1"
                    title={`Click to edit manually\n${fullRoot}`}
                    onClick={() => {
                      setRootInput(fullRoot);
                      setEditingRoot(true);
                    }}
                  >
                    {rootDisplay}
                  </span>
                  <button
                    className="mono text-[9px] text-faint hover:text-signal transition-colors shrink-0"
                    onClick={() => setShowPicker(true)}
                    title="Browse directories to change workspace root"
                  >
                    [BROWSE]
                  </button>
                </>
              )}
            </div>
          }
        />
        <div className="flex items-center justify-between border-b border-edge-soft py-1">
          <span className="label !text-[10px]">CEPHALON PROTOCOL</span>
          <span className="flex items-center gap-2 mono text-[9px]">
            <span
              className={cn(
                "pip",
                health?.workspace?.level === "full"
                  ? "pip--go"
                  : health?.workspace?.level === "partial"
                  ? "pip--hazard"
                  : "pip--crit"
              )}
            />
            <span className="uppercase text-phosphor-dim">{health?.workspace?.level ?? "NONE"}</span>
          </span>
        </div>
        {health?.workspace?.checks && (
          <div className="mt-1 space-y-1.5">
            {CEPHALON_CHECKS.map(([label, key]) => {
              const ok = health.workspace!.checks[key];
              return (
                <div key={key} className="mono flex items-center justify-between text-[9px]">
                  <span className="text-faint" title={label}>{label}</span>
                  <button
                    className={cn(ok ? "text-go" : "text-critical hover:text-signal transition-colors cursor-pointer")}
                    title={ok
                      ? `${label} — found`
                      : `${label} — missing from workspace. Click to re-scan.`}
                    onClick={() => void init()}
                  >
                    {ok ? "✓" : "✗"}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      {showPicker && (
        <FsBrowserModal
          initialPath={fullRoot}
          onSelect={async (path) => {
            try {
              await setWorkspaceRoot(path);
              await init();
              setShowPicker(false);
            } catch {
              // error is shown in browser or console
            }
          }}
          onCancel={() => setShowPicker(false)}
        />
      )}
    </aside>
  );
}
