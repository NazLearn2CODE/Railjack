import { useState, type KeyboardEvent } from "react";
import { useStore } from "../store";
import { cn } from "../util";

export default function Composer() {
  const composing = useStore((s) => s.composing);
  const setComposing = useStore((s) => s.setComposing);
  const dispatch = useStore((s) => s.dispatch);
  const mode = useStore((s) => s.mode);
  const setMode = useStore((s) => s.setMode);
  const t = useStore((s) => (s.activeId ? s.transcripts[s.activeId] : undefined));
  const [sysOpen, setSysOpen] = useState(false);
  const [sys, setSys] = useState("");

  const team = mode === "team";
  const busy =
    t?.status === "running" ||
    t?.status === "pending_admission" ||
    t?.status === "waiting_approval";

  const send = () => {
    if (busy || !composing.trim()) return;
    void dispatch(composing, sys || undefined);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="shrink-0 border-t border-edge bg-panel/60 px-4 py-3">
      {sysOpen && (
        <input
          className="input mb-2 !py-2 text-[12px]"
          placeholder="system prompt override (optional)…"
          value={sys}
          onChange={(e) => setSys(e.target.value)}
        />
      )}
      <div className="flex items-end gap-2">
        <span className="display pb-2 text-[13px] text-hazard">▸</span>
        <textarea
          className="input min-h-[44px] resize-none"
          rows={2}
          placeholder={team ? "DISPATCH A TASK — SUPERVISOR DELEGATES…" : "DISPATCH A PROMPT TO THE AGENT…"}
          value={composing}
          onChange={(e) => setComposing(e.target.value)}
          onKeyDown={onKey}
          disabled={busy}
        />
        <button
          className={cn("btn btn--signal shrink-0")}
          onClick={send}
          disabled={busy || !composing.trim()}
        >
          {busy ? "·· BUSY" : team ? "DEPLOY" : "DISPATCH"}
        </button>
      </div>
      <div className="mt-1.5 flex items-center justify-between text-[9px] text-faint">
        <span>ENTER ↵ {team ? "DEPLOY TEAM" : "DISPATCH"} · SHIFT+ENTER NEWLINE</span>
        <div className="flex items-center gap-3">
          <button
            className={cn("transition-colors hover:text-signal", team && "text-signal")}
            onClick={() => setMode(team ? "single" : "team")}
          >
            {team ? "● TEAM" : "○ TEAM"}
          </button>
          <button className="transition-colors hover:text-signal" onClick={() => setSysOpen((v) => !v)}>
            {sysOpen ? "− HIDE SYS" : "+ SYS PROMPT"}
          </button>
        </div>
      </div>
    </div>
  );
}
