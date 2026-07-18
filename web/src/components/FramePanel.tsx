import type { FC } from "react";
import { useStore, type ModuleConfig } from "../store";
import ManageBar from "./ManageBar";

export default function FramePanel({ panels }: { panels: Record<string, FC<{ module: ModuleConfig }>> }) {
  const modules = useStore((s) => s.config?.modules) ?? [];
  const activeId = useStore((s) => s.activeModuleId);
  const active = modules.find((m) => m.id === activeId) ?? null;
  const iframeMods = modules.filter((m) => m.kind === "iframe");
  const Panel = active?.panel ? panels[active.panel] : undefined;

  return (
    <section className="hud hud--glass hud--bracket reveal reveal-3 m-2 flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-edge bg-panel px-3 py-2">
        <span className="label">
          <span className="text-signal">▸</span> {active?.title ?? "—"}
        </span>
        <span className="label">{active?.kind.toUpperCase()}</span>
      </div>

      {/* ManageBar is chrome above the frame area (never inside the iframe map),
          so toggling it can't remount the tmux terminal session. */}
      {active?.manage && <ManageBar moduleId={active.id} />}

      <div className="relative min-h-0 flex-1">
        {/*
          CRITICAL: every iframe module is rendered ONCE here and toggled via
          CSS display. We never conditionally unmount on module switch — a
          remount reloads the tmux terminal session (the #1 regression).
        */}
        {iframeMods.map((m) => (
          <iframe
            key={m.id}
            src={m.url}
            title={m.title}
            className="absolute inset-0 h-full w-full border-0"
            style={{ display: activeId === m.id ? "block" : "none" }}
          />
        ))}

        {/* Panel modules: render their registered component, else a placeholder. */}
        {active?.kind === "panel" &&
          (Panel ? (
            <div className="absolute inset-0 overflow-hidden">
              <Panel module={active} />
            </div>
          ) : (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-center">
                <div className="panel-title mb-2">{active.title}</div>
                <div className="label">COMING SOON</div>
              </div>
            </div>
          ))}
      </div>
    </section>
  );
}
