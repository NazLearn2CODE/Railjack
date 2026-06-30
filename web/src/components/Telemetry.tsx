import type { ReactNode } from "react";
import { useStore } from "../store";
import { cn, connMeta } from "../util";
import type { LogEntry } from "../store";

function Panel({ idx, title, children }: { idx: string; title: string; children: ReactNode }) {
  return (
    <div className="border border-edge bg-void/60">
      <div className="border-b border-edge-soft px-2.5 py-1.5">
        <span className="label">
          <span className="text-signal">{idx}</span> / {title}
        </span>
      </div>
      <div className="px-2.5 py-2">{children}</div>
    </div>
  );
}

function Readout({ label, value, muted }: { label: string; value: string; muted?: boolean }) {
  return (
    <div className="flex items-center justify-between border-b border-edge-soft py-1 last:border-0">
      <span className="label !text-[9px]">{label}</span>
      <span
        className={cn("mono truncate text-[10px]", muted ? "text-faint" : "text-phosphor-dim")}
        style={{ maxWidth: "55%" }}
      >
        {value}
      </span>
    </div>
  );
}

const toneColor: Record<LogEntry["tone"], string> = {
  signal: "var(--color-signal)",
  hazard: "var(--color-hazard)",
  go: "var(--color-go)",
  crit: "var(--color-critical)",
  muted: "var(--color-muted)",
};
const tonePip: Record<LogEntry["tone"], string> = {
  signal: "pip--signal",
  hazard: "pip--hazard",
  go: "pip--go",
  crit: "pip--crit",
  muted: "",
};

export default function Telemetry() {
  const log = useStore((s) => s.log);
  const conn = useStore((s) => s.conn);
  const activeId = useStore((s) => s.activeId);
  const t = useStore((s) => (s.activeId ? s.transcripts[s.activeId] : undefined));
  const c = connMeta(conn);

  return (
    <aside className="reveal reveal-4 m-2 ml-1 flex min-h-0 flex-col gap-2.5 overflow-y-auto p-1">
      <Panel idx="02" title="SESSION">
        <Readout label="ID" value={activeId ? activeId.slice(0, 13) + "…" : "—"} />
        <Readout label="STATUS" value={(t?.status ?? "—").toUpperCase()} />
        <Readout label="TOKENS" value={String(t?.tokens ?? 0)} />
        <Readout label="ROWS" value={String(t?.rows.length ?? 0)} />
        <Readout label="LINK" value={c.label} />
      </Panel>

      <Panel idx="03" title="SCHEDULER">
        <Readout label="ADMISSION" value="—" muted />
        <Readout label="AIMD CONC" value="—" muted />
        <Readout label="BREAKER" value="—" muted />
        <Readout label="RPM / TPM" value="—" muted />
        <p className="mt-2 text-[9px] leading-relaxed text-faint">
          HiveMind telemetry awaits a read endpoint on the gateway.
        </p>
      </Panel>

      <Panel idx="04" title="EVENT STREAM">
        <div className="max-h-[240px] overflow-y-auto">
          {log.length === 0 ? (
            <div className="label py-1 text-faint">NO EVENTS</div>
          ) : (
            log.map((e) => (
              <div
                key={e.id}
                className="tick mono flex items-center gap-2 border-b border-edge-soft py-1 text-[10px]"
              >
                <span className={cn("pip", tonePip[e.tone])} style={{ width: 5, height: 5 }} />
                <span style={{ color: toneColor[e.tone] }}>{e.label}</span>
              </div>
            ))
          )}
        </div>
      </Panel>
    </aside>
  );
}
