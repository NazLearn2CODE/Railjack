import { useStore } from "../store";

export default function FramePanel() {
  const modules = useStore((s) => s.config?.modules) ?? [];
  const activeId = useStore((s) => s.activeModuleId);
  const active = modules.find((m) => m.id === activeId) ?? null;
  const iframeMods = modules.filter((m) => m.kind === "iframe");

  return (
    <section className="hud hud--bracket reveal reveal-3 m-2 flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-edge bg-panel px-3 py-2">
        <span className="label">
          <span className="text-signal">▸</span> {active?.title ?? "—"}
        </span>
        <span className="label">{active?.kind.toUpperCase()}</span>
      </div>

      <div className="relative min-h-0 flex-1 bg-void">
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
            className="absolute inset-0 h-full w-full border-0 bg-white"
            style={{ display: activeId === m.id ? "block" : "none" }}
          />
        ))}

        {/* Panel modules render a placeholder until their panel lands (M4). */}
        {active?.kind === "panel" && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-center">
              <div className="panel-title mb-2">{active.title}</div>
              <div className="label">COMING SOON</div>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
