import { useEffect } from "react";
import { useStore } from "./store";
import TopBar from "./components/TopBar";
import Sidebar from "./components/Sidebar";
import Console from "./components/Console";
import Telemetry from "./components/Telemetry";

export default function App() {
  const init = useStore((s) => s.init);
  useEffect(() => {
    void init();
  }, [init]);

  return (
    <div className="field relative h-screen w-screen overflow-hidden">
      <div className="scanlines" />
      <div className="grain" />
      <div className="relative z-10 flex h-full flex-col">
        <TopBar />
        <main className="grid min-h-0 flex-1" style={{ gridTemplateColumns: "312px 1fr 308px" }}>
          <Sidebar />
          <Console />
          <Telemetry />
        </main>
      </div>
    </div>
  );
}
