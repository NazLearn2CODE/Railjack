import { useCallback, useEffect, useRef, useState } from "react";
import { useStore } from "./store";
import TopBar from "./components/TopBar";
import Sidebar from "./components/Sidebar";
import Console from "./components/Console";
import Telemetry from "./components/Telemetry";

const MIN_COL = 180;

export default function App() {
  const init = useStore((s) => s.init);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [cols, setCols] = useState({ left: 312, right: 308 });
  const drag = useRef<"left" | "right" | null>(null);
  const startX = useRef(0);
  const startW = useRef(0);

  useEffect(() => {
    void init();
  }, [init]);

  const onMouseDown = useCallback((side: "left" | "right", e: React.MouseEvent) => {
    e.preventDefault();
    drag.current = side;
    startX.current = e.clientX;
    startW.current = side === "left" ? cols.left : cols.right;
  }, [cols.left, cols.right]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!drag.current) return;
      const dx = e.clientX - startX.current;
      if (drag.current === "left") {
        setCols((c) => ({ ...c, left: Math.max(MIN_COL, startW.current + dx) }));
      } else {
        setCols((c) => ({ ...c, right: Math.max(MIN_COL, startW.current - dx) }));
      }
    };
    const onUp = () => {
      drag.current = null;
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  return (
    <div className="field relative h-screen w-screen overflow-hidden">
      <div className="scanlines" />
      <div className="grain" />
      <div className="relative z-10 flex h-full flex-col">
        <TopBar />
        <main className="grid min-h-0 flex-1" style={{ gridTemplateColumns: `${sidebarCollapsed ? 48 : cols.left}px 4px 1fr 4px ${cols.right}px` }}>
          <Sidebar collapsed={sidebarCollapsed} onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)} />
          {/* left drag handle */}
          {!sidebarCollapsed && (
            <div
              className="cursor-col-resize bg-edge/40 hover:bg-signal/30 transition-colors"
              onMouseDown={(e) => onMouseDown("left", e)}
            />
          )}
          <div className="flex flex-col min-w-0 min-h-0 h-full">
            <Console />
          </div>
          {/* right drag handle */}
          <div
            className="cursor-col-resize bg-edge/40 hover:bg-signal/30 transition-colors"
            onMouseDown={(e) => onMouseDown("right", e)}
          />
          <Telemetry />
        </main>
      </div>
    </div>
  );
}
