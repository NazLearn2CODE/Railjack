import { useState } from "react";
import { useStore } from "../store";
import { cn, shortId, statusMeta } from "../util";
import SkillsList from "./SkillsList";
import ServicesList from "./ServicesList";

export default function Sidebar() {
  const sessions = useStore((s) => s.sessions);
  const activeId = useStore((s) => s.activeId);
  const transcripts = useStore((s) => s.transcripts);
  const select = useStore((s) => s.select);
  const init = useStore((s) => s.init);
  const [tab, setTab] = useState<"agents" | "skills" | "services">("agents");

  return (
    <aside className="reveal reveal-2 hud hud--bracket m-2 mr-1 flex min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-edge px-3 py-2">
        <div className="flex items-center gap-2 label text-[10px]">
          <span className="text-signal">01</span> /
          <button
            onClick={() => setTab("agents")}
            className={cn("hover:text-signal transition-colors uppercase font-bold px-1 py-0.5", tab === "agents" && "text-signal border-b border-signal")}
          >
            AGENTS
          </button>
          <button
            onClick={() => setTab("skills")}
            className={cn("hover:text-signal transition-colors uppercase font-bold px-1 py-0.5", tab === "skills" && "text-signal border-b border-signal")}
          >
            SKILLS
          </button>
          <button
            onClick={() => setTab("services")}
            className={cn("hover:text-signal transition-colors uppercase font-bold px-1 py-0.5", tab === "services" && "text-signal border-b border-signal")}
          >
            SERVICES
          </button>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {tab === "agents" && <span className="mono text-[10px] text-faint">{sessions.length} REC</span>}
          <button className="btn !px-2 !py-1 !text-[9px]" onClick={() => void init()} title="refresh">
            ↻
          </button>
        </div>
      </div>

      {tab === "agents" && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sessions.length === 0 ? (
            <div className="px-3 py-6 text-center">
              <div className="label mb-2">NO SESSIONS</div>
              <p className="text-[11px] leading-relaxed text-faint">
                Dispatch a prompt from the console to spawn an agent process.
              </p>
            </div>
          ) : (
            sessions.map((s) => {
              const meta = statusMeta(
                transcripts[s.session_id]?.status ?? s.status,
              );
              const active = s.session_id === activeId;
              return (
                <button
                  key={s.session_id}
                  onClick={() => void select(s.session_id)}
                  className={cn(
                    "block w-full border-b border-edge-soft px-3 py-2.5 text-left transition-colors",
                    active ? "bg-signal/10" : "hover:bg-panel-2",
                  )}
                >
                  <div className="mb-1 flex items-center justify-between gap-2">
                    <span className="flex items-center gap-2">
                      <span className={cn("pip", meta.pip)} />
                      <span
                        className="display text-[10px] font-semibold tracking-[0.14em]"
                        style={{ color: meta.color }}
                      >
                        {meta.label}
                      </span>
                    </span>
                    <span className="mono text-[9px] text-faint">{shortId(s.session_id)}</span>
                  </div>
                  <div className="truncate text-[12px] text-phosphor-dim">{s.prompt}</div>
                  <div className="mt-1 flex items-center gap-3 text-[9px] text-faint">
                    <span>
                      <span className="text-muted">TK</span> {transcripts[s.session_id]?.tokens ?? s.tokens_consumed}
                    </span>
                    {s.error && <span className="text-critical">ERR</span>}
                  </div>
                </button>
              );
            })
          )}
        </div>
      )}

      {tab === "skills" && <SkillsList />}
      {tab === "services" && <ServicesList />}
    </aside>
  );
}
