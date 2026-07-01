import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useStore } from "../store";
import type { Row } from "../types";
import { cn, statusMeta } from "../util";
import ApprovalCard from "./ApprovalCard";

function safeJson(v: unknown): string {
  try {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export default function Message({ row }: { row: Row }) {
  // Called unconditionally (Rules of Hooks); only USED in the worker_lane case.
  // Action reference is stable, so non-worker rows never re-render on store changes.
  const approveWorker = useStore((s) => s.approveWorker);
  switch (row.kind) {
    case "user":
      return (
        <div className="row-in flex justify-end">
          <div className="max-w-[85%] border border-edge bg-panel-2 px-3 py-2">
            <div className="label mb-1 flex justify-end">
              <span className="text-hazard">OPERATOR</span>
            </div>
            <div className="whitespace-pre-wrap text-[13px] text-phosphor">{row.text}</div>
          </div>
        </div>
      );

    case "text":
      return (
        <div className="row-in">
          <div className="label mb-1.5">
            <span className="text-signal">AGENT</span> · RESPONSE
          </div>
          <div className="prose-md border-l-2 border-signal/40 pl-3">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{row.text}</ReactMarkdown>
          </div>
        </div>
      );

    case "thinking":
      return (
        <details className="row-in border border-edge-soft bg-void px-3 py-2">
          <summary className="label cursor-pointer select-none">▸ COGNITION TRACE</summary>
          <div className="mt-2 whitespace-pre-wrap text-[12px] italic text-muted">{row.text}</div>
        </details>
      );

    case "tool_use":
      return (
        <div className="row-in border border-edge-soft bg-void px-3 py-2">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="label">
              <span className="text-hazard">TOOL</span> · {row.name}
            </span>
            <span className="label text-faint">EXECUTED</span>
          </div>
          <pre className="overflow-x-auto text-[11px] leading-relaxed text-phosphor-dim">{safeJson(row.input)}</pre>
        </div>
      );

    case "result":
      return (
        <div
          className={cn(
            "row-in mt-1 flex items-center gap-2 border-t pt-2",
            row.isError ? "border-critical/40" : "border-go/30",
          )}
        >
          <span className={cn("pip", row.isError ? "pip--crit" : "pip--go")} />
          <span
            className="label"
            style={{ color: row.isError ? "var(--color-critical)" : "var(--color-go)" }}
          >
            {row.isError ? "RUN FAULT" : "RUN COMPLETE"}
          </span>
          <span className="truncate text-[11px] text-muted">{row.text}</span>
        </div>
      );

    case "worker_lane": {
      // A delegated worker's run, inlined where the supervisor called delegate.
      const meta = statusMeta(row.status);
      return (
        <div className="row-in border border-edge-soft bg-void/60 px-3 py-2.5">
          <div className="mb-2 flex items-center justify-between border-b border-edge-soft pb-1.5">
            <span className="label">
              <span className="text-signal">◂ DELEGATED</span> · {row.role}
            </span>
            <span className="label flex items-center gap-1.5">
              <span className={cn("pip", meta.pip)} />
              <span style={{ color: meta.color }}>{meta.label}</span>
            </span>
          </div>
          <div className="space-y-2 border-l border-edge-soft pl-3">
            {row.rows.length === 0 && <div className="label text-faint">WORKER SPUN UP…</div>}
            {row.rows.map((r, i) => (
              <Message key={i} row={r} />
            ))}
            {row.approval && (
              <ApprovalCard
                name={row.approval.tool}
                input={row.approval.input}
                onResolve={(a) => void approveWorker(row.workerId, row.approval!.approvalId, a)}
              />
            )}
          </div>
        </div>
      );
    }
  }
}
