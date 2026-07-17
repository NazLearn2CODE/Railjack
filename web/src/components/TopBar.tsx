import { useEffect, useState } from "react";
import { useStore } from "../store";

export default function TopBar() {
  const machine = useStore((s) => s.config?.machine);
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const clock = now.toLocaleTimeString("en-GB"); // HH:MM:SS 24h

  return (
    <header className="hud hud--bracket reveal reveal-1 m-2 mb-0 flex items-center justify-between px-4 py-2">
      <div className="flex items-center gap-3">
        <span className="panel-title">▸ RAILJACK</span>
        <span className="label">// {(machine ?? "—").toUpperCase()}</span>
      </div>
      <div className="flex items-center gap-3">
        <span className="label">UPLINK</span>
        <span className="pip" aria-hidden />
        <span className="mono text-signal">{clock}</span>
      </div>
    </header>
  );
}
