import { useEffect } from "react";
import { fetchJSON } from "./api";
import { useStore, type AppConfig } from "./store";
import TopBar from "./components/TopBar";
import ModuleRail from "./components/ModuleRail";
import FramePanel from "./components/FramePanel";

export default function App() {
  const setConfig = useStore((s) => s.setConfig);
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

  // Auto-select the first module once config lands.
  useEffect(() => {
    if (config && config.modules.length && !useStore.getState().activeModuleId) {
      useStore.getState().setActive(config.modules[0].id);
    }
  }, [config]);

  return (
    <div className="field relative h-full w-full">
      <div className="scanlines" />
      <div className="grain" />
      <div className="relative z-10 flex h-full w-full flex-col">
        <TopBar />
        <div className="flex min-h-0 flex-1">
          <ModuleRail />
          <FramePanel />
        </div>
      </div>
    </div>
  );
}
