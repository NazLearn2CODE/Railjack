import { useEffect, useState } from "react";
import { useStore } from "../store";
import { clock, cn, connMeta } from "../util";

export default function TopBar() {
  const conn = useStore((s) => s.conn);
  const [t, setT] = useState(() => new Date());
  useEffect(() => {
    const i = setInterval(() => setT(new Date()), 1000);
    return () => clearInterval(i);
  }, []);
  const c = connMeta(conn);

  return (
    <header className="reveal reveal-1 flex shrink-0 items-center justify-between border-b border-edge bg-panel/60 px-4 py-2.5">
      <div className="flex items-center gap-3">
        <svg width="26" height="26" viewBox="0 0 26 26" fill="none" className="shrink-0">
          <circle cx="13" cy="13" r="2.5" fill="var(--color-signal)" />
          <ellipse cx="13" cy="13" rx="11" ry="4.5" stroke="var(--color-signal)" strokeOpacity="0.55" />
          <ellipse
            cx="13"
            cy="13"
            rx="11"
            ry="4.5"
            stroke="var(--color-signal)"
            strokeOpacity="0.35"
            transform="rotate(60 13 13)"
          />
          <circle cx="24" cy="9" r="1.4" fill="var(--color-hazard)" />
        </svg>
        <div className="leading-none">
          <div className="display text-[11px] font-bold tracking-[0.34em] text-phosphor">ORBITER</div>
          <div className="label mt-1">AGENTIC OS · CONSOLE · v0.1</div>
        </div>
      </div>
      <div className="flex items-center gap-7">
        <div className="flex items-center gap-2">
          <span className={cn("pip", c.pip)} />
          <span className="label" style={{ color: "var(--color-phosphor-dim)" }}>
            {c.label}
          </span>
        </div>
        <div className="mono text-[11px] tabular-nums text-muted">
          <span className="text-faint">T+</span> {clock(t)}
        </div>
      </div>
    </header>
  );
}
