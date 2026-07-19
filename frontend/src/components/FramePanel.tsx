import type { FC } from "react";
import { useStore, dockOpenFor, type ModuleConfig } from "../store";
import CockpitControls from "./CockpitControls";

export default function FramePanel({ panels }: { panels: Record<string, FC<{ module: ModuleConfig }>> }) {
  const modules = useStore((s) => s.config?.modules) ?? [];
  const activeId = useStore((s) => s.activeModuleId);
  const active = modules.find((m) => m.id === activeId) ?? null;
  const iframeMods = modules.filter((m) => m.kind === "iframe");
  const Panel = active?.panel ? panels[active.panel] : undefined;

  // LIVE dock toggle. The button only appears when a `dock:` is configured for
  // this machine; it flips the dock open/closed for the current module. Its
  // highlighted state mirrors whether the dock is currently showing.
  const hasDock = useStore((s) => Boolean(s.config?.dock));
  const dockOpen = useStore((s) => s.dockOpen);
  const toggleDock = useStore((s) => s.toggleDock);
  const liveOn = dockOpenFor(dockOpen, active ?? undefined);

  return (
    <section className="hud hud--glass hud--bracket reveal reveal-3 m-2 flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-2 flex-wrap border-b border-edge bg-panel px-3 py-1.5">
        <div className="flex items-center gap-2">
          <span className="panel-title">▸ {active?.title ?? "—"}</span>
          {hasDock && active && (
            <button
              onClick={() => toggleDock(active.id)}
              // btn--compact matches the BOOTSTRAP button's height (3px/8px pad,
              // 11px font). Highlighted (btn--signal) when open, dim otherwise.
              className={`btn btn--compact hud--bracket flex items-center gap-1.5 ${liveOn ? "btn--signal" : "opacity-60"}`}
              title={liveOn ? "Hide the LIVE terminal" : "Show the LIVE terminal"}
              aria-pressed={liveOn}
            >
              <span className={liveOn ? "pip pip--go" : "pip"} aria-hidden />
              <span>LIVE</span>
            </button>
          )}
        </div>
        <CockpitControls />
      </div>

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
