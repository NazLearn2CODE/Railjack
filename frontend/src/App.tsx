import { useEffect, memo, useState } from "react";
import type { FC } from "react";
import { fetchJSON, usePolling } from "./api";
import { useStore, dockOpenFor, type AppConfig, type ModuleConfig } from "./store";
import ModuleRail from "./components/ModuleRail";
import FramePanel from "./components/FramePanel";
import FfmpegPanel from "./components/FfmpegPanel";
import ComfyPanel from "./components/ComfyPanel";
import NotebookPanel from "./components/NotebookPanel";
import NewsroomPanel from "./components/NewsroomPanel";
import ThailandNowPanel from "./components/ThailandNowPanel";
import KanbanPanel from "./components/KanbanPanel";
import FiresideStudioPanel from "./components/FiresideStudioPanel";

// kind:"panel" modules render their panel component from this map.
const PANELS: Record<string, FC<{ module: ModuleConfig }>> = {
  ffmpeg: FfmpegPanel,
  comfyui: ComfyPanel,
  notebooklm: NotebookPanel,
  newsroom: NewsroomPanel,
  thailandnow: ThailandNowPanel,
  kanban: KanbanPanel,
  fireside: FiresideStudioPanel,
};

// Phase F: always-visible bottom terminal dock. Memoized so the frequent health
// polls that re-render App never re-run this — the iframe mounts ONCE (a remount
// would reload the tmux client). Same ttyd URL as TERMINAL → shared session.
const LiveDock = memo(function LiveDock({
  title,
  url,
  height,
}: {
  title: string;
  url: string;
  height: number;
}) {
  // The dock iframe is pinned (memo, stable key) so health-poll re-renders never
  // drop the tmux session. A ttyd restart leaves that one iframe stuck on the
  // disconnect frame; this ↻ bumps key → forced remount → fresh reconnect, without
  // reloading the whole dashboard. tmux `new -A` reattaches the same session (no loss).
  const [reloadKey, setReloadKey] = useState(0);
  return (
    <section
      className="hud hud--glass hud--bracket m-2 mt-0 flex shrink-0 flex-col overflow-hidden"
      style={{ height }}
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-1">
        <span className="panel-title">▸ {title}</span>
        <button
          className="btn btn--compact ml-auto"
          title="Reconnect terminal — reloads the tmux client (use if the dock goes blank/stale)"
          onClick={() => setReloadKey((k) => k + 1)}
        >
          ↻
        </button>
      </div>
      <div className="relative min-h-0 flex-1 bg-black">
        <iframe key={reloadKey} src={url} title={title} className="absolute inset-0 h-full w-full border-0" />
      </div>
    </section>
  );
});

export default function App() {
  const setConfig = useStore((s) => s.setConfig);
  const setHealth = useStore((s) => s.setHealth);
  const config = useStore((s) => s.config);
  const activeModuleId = useStore((s) => s.activeModuleId);
  const dockOpen = useStore((s) => s.dockOpen);

  useEffect(() => {
    let alive = true;
    fetchJSON<AppConfig>("/api/config")
      .then((c) => alive && setConfig(c))
      .catch((e: unknown) => console.error("config load failed", e));
    return () => {
      alive = false;
    };
  }, [setConfig]);

  // Health pips: poll every 5 s.
  const { data: health } = usePolling<Record<string, string>>("/api/health", 5000);
  useEffect(() => {
    if (health) setHealth(health);
  }, [health, setHealth]);

  // Reconcile the amber "STARTING…" overlay: a module that has gone healthy is no
  // longer starting; expired deadlines fall back to raw health on a 1 s tick.
  const starting = useStore((s) => s.starting);
  useEffect(() => {
    const s = useStore.getState();
    for (const id of Object.keys(s.starting)) {
      if (s.healthMap[id] === "ok") s.clearStarting(id);
    }
  }, [health]);
  useEffect(() => {
    if (Object.keys(starting).length === 0) return;
    const t = setInterval(() => {
      const s = useStore.getState();
      const now = Date.now();
      for (const [id, deadline] of Object.entries(s.starting)) {
        if (now >= deadline) s.clearStarting(id);
      }
    }, 1000);
    return () => clearInterval(t);
  }, [starting]);

  // Auto-select once config lands: a matching URL hash (#<module-id>) wins,
  // else the first module. Lets you deep-link/refresh straight into a panel.
  useEffect(() => {
    if (config && config.modules.length && !useStore.getState().activeModuleId) {
      const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
      const fromHash = config.modules.find((m) => m.id === hash);
      useStore.getState().setActive(fromHash ? fromHash.id : config.modules[0].id);
    }
  }, [config]);

  // LIVE dock renders when a top-level `dock:` is configured AND the top-bar
  // LIVE button is toggled on for the active module. The initial per-module
  // state comes from the `live_dock` YAML default (n8n open, others closed);
  // the button (in FramePanel) lets Naz override it live on any module.
  const activeModule = config?.modules.find((m) => m.id === activeModuleId);
  const showDock = Boolean(config?.dock) && dockOpenFor(dockOpen, activeModule);

  return (
    <div className="field relative h-full w-full">
      <div className="scanlines" />
      <div className="grain" />
      <div className="relative z-10 flex h-full w-full flex-col">
        <div className="flex min-h-0 flex-1">
          <ModuleRail />
          <FramePanel panels={PANELS} />
        </div>
        {showDock && config?.dock && (
          <LiveDock
            title={config.dock.title}
            url={config.dock.url}
            height={config.dock.height}
          />
        )}
      </div>
    </div>
  );
}
