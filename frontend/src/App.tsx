import { useEffect } from "react";
import type { FC } from "react";
import { fetchJSON, usePolling } from "./api";
import { useStore, type AppConfig, type ModuleConfig } from "./store";
import ModuleRail from "./components/ModuleRail";
import FramePanel from "./components/FramePanel";
import FfmpegPanel from "./components/FfmpegPanel";
import ComfyPanel from "./components/ComfyPanel";
import NotebookPanel from "./components/NotebookPanel";

// kind:"panel" modules render their panel component from this map.
const PANELS: Record<string, FC<{ module: ModuleConfig }>> = {
  ffmpeg: FfmpegPanel,
  comfyui: ComfyPanel,
  notebooklm: NotebookPanel,
};

export default function App() {
  const setConfig = useStore((s) => s.setConfig);
  const setHealth = useStore((s) => s.setHealth);
  const config = useStore((s) => s.config);

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

  return (
    <div className="field relative h-full w-full">
      <div className="scanlines" />
      <div className="grain" />
      <div className="relative z-10 flex h-full w-full flex-col">
        <div className="flex min-h-0 flex-1">
          <ModuleRail />
          <FramePanel panels={PANELS} />
        </div>
      </div>
    </div>
  );
}
