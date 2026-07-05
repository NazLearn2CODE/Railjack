import { useEffect, useRef } from "react";
import { useStore } from "../store";
import { cn, shortId, statusMeta } from "../util";
import ApprovalCard from "./ApprovalCard";
import Composer from "./Composer";
import Message from "./Message";
import ServiceFrame from "./ServiceFrame";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex items-baseline gap-1.5">
      <span className="label !text-[11px]">{label}</span>
      <span className="mono text-[11px] text-phosphor-dim tabular-nums">{value}</span>
    </span>
  );
}

export default function Console() {
  const activeId = useStore((s) => s.activeId);
  const t = useStore((s) => (s.activeId ? s.transcripts[s.activeId] : undefined));
  const approve = useStore((s) => s.approve);
  const embed = useStore((s) => s.embed);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [t?.rows.length, activeId]);

  if (embed) {
    return (
      <section className="reveal reveal-3 hud hud--bracket m-2 mx-1 flex min-h-0 flex-col">
        <ServiceFrame />
      </section>
    );
  }

  if (!activeId || !t) {
    return (
      <section className="reveal reveal-3 hud hud--bracket m-2 mx-1 flex min-h-0 flex-col">
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 px-6 text-center">
          <svg width="46" height="46" viewBox="0 0 46 46" fill="none" className="opacity-70">
            <circle cx="23" cy="23" r="4" fill="var(--color-signal)" />
            <ellipse cx="23" cy="23" rx="20" ry="8" stroke="var(--color-signal)" strokeOpacity="0.4" />
            <ellipse
              cx="23"
              cy="23"
              rx="20"
              ry="8"
              stroke="var(--color-signal)"
              strokeOpacity="0.25"
              transform="rotate(60 23 23)"
            />
          </svg>
          <div className="display text-[10px] font-semibold tracking-[0.24em] text-phosphor-dim">
            NO ACTIVE SESSION
          </div>
          <p className="max-w-[320px] text-[11px] leading-relaxed text-faint">
            Dispatch a prompt below to spawn an agent process under HiveMind scheduling.
            Output streams here in real time.
          </p>
        </div>
        <Composer />
      </section>
    );
  }

  const meta = statusMeta(t.status);
  const live = t.status === "running" || t.status === "pending_admission";
  const pending = Object.entries(t.pendingTools);

  return (
    <section className="reveal reveal-3 hud hud--bracket m-2 mx-1 flex min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-edge px-4 py-2.5">
        <div className="flex items-center gap-3">
          <span className={cn("pip", meta.pip)} />
          <span
            className="display text-[11px] font-semibold tracking-[0.16em]"
            style={{ color: meta.color }}
          >
            {meta.label}
          </span>
          <span className="mono text-[10px] text-faint">SESSION {shortId(activeId)}</span>
        </div>
        <div className="flex items-center gap-5">
          <Stat label="TK" value={String(t.tokens)} />
          <Stat label="ROWS" value={String(t.rows.length)} />
          <Stat label="PHASE" value={meta.label} />
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {t.rows.length === 0 && <div className="label text-faint">AWAITING DISPATCH…</div>}
        {t.rows.map((row, i) => (
          <Message key={i} row={row} />
        ))}
        {pending.map(([aid, p]) => (
          <ApprovalCard
            key={aid}
            name={p.name}
            input={p.input}
            onResolve={(a) => void approve(aid, a)}
          />
        ))}
        {live && <div className="label text-signal caret">STREAMING</div>}
        <div ref={endRef} />
      </div>

      <Composer />
    </section>
  );
}
