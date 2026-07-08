import { useState } from "react";
import { useStore } from "../store";
import { cn, shortId, statusMeta } from "../util";
import SkillsList from "./SkillsList";
import ServicesList from "./ServicesList";



export default function Sidebar({
  collapsed,
  onToggleCollapse,
}: {
  collapsed: boolean;
  onToggleCollapse: () => void;
}) {
  const sessions = useStore((s) => s.sessions);
  const activeId = useStore((s) => s.activeId);
  const transcripts = useStore((s) => s.transcripts);
  const select = useStore((s) => s.select);
  const init = useStore((s) => s.init);
  const providers = useStore((s) => s.providers);
  const selectedModel = useStore((s) => s.model);
  const setModel = useStore((s) => s.setModel);
  const [tab, setTab] = useState<"sessions" | "skills" | "services">("sessions");

  // Draft (pending APPLY) model selection — decoupled from the store's applied
  // model so the operator can stage a switch and confirm it deliberately. The
  // applied model is the single source of truth for what the next dispatch runs.
  const [draft, setDraft] = useState<{ provider: string; model: string } | null>(selectedModel);
  const dirty = !!draft && !!selectedModel &&
    (draft.provider !== selectedModel.provider || draft.model !== selectedModel.model);

  if (collapsed) {
    return (
      <aside className="reveal reveal-2 hud hud--bracket m-2 mr-1 flex w-[48px] min-h-0 flex-col items-center py-3 bg-panel/30 border border-edge shrink-0 select-none">
        <button
          className="btn !px-1.5 !py-0.5 !text-[12px] hover:text-signal transition-colors mb-6 shrink-0"
          onClick={onToggleCollapse}
          title="Expand sidebar"
        >
          ▶
        </button>
        <div
          className="flex-1 flex flex-col items-center justify-center gap-10 text-faint font-mono text-[9px] tracking-[0.2em] uppercase"
          style={{ writingMode: "vertical-rl", textOrientation: "mixed" }}
        >
          <span>SESSIONS</span>
          <span>SKILLS</span>
          <span>SERVICES</span>
        </div>
      </aside>
    );
  }

  const flatModels: { provider: string; model: string | null; label: string }[] = [];
  for (const p of providers) {
    if (p.models.length === 0) {
      flatModels.push({ provider: p.name, model: null, label: p.name });
    } else {
      for (const m of p.models) {
        flatModels.push({ provider: p.name, model: m, label: `${p.name} / ${m}` });
      }
    }
  }

  const handleDeleteSession = async (sessionId: string) => {
    try {
      const res = await fetch(`/api/sessions/${sessionId}`, {
        method: "DELETE"
      });
      if (res.ok) {
        if (activeId === sessionId) {
          useStore.setState({ activeId: null });
        }
        await init();
      }
    } catch (e) {
      console.error(e);
    }
  };

  return (
    <aside className="reveal reveal-2 hud hud--bracket m-2 mr-1 flex min-h-0 flex-col">
      {/* Model selector — always visible at top. Draft + APPLY: picking a model
          stages it; APPLY commits it to the store (the source of truth for the
          next dispatch). The active readout below shows what's actually applied. */}
      <div className="border-b border-edge px-3 py-2 shrink-0">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0 flex-1">
            <span className="pip pip--signal shrink-0" />
            <select
              value={draft ? `${draft.provider}:${draft.model}` : ""}
              onChange={(e) => {
                const val = e.target.value;
                if (!val) {
                  setDraft(null);
                } else {
                  const [pName, mName] = val.split(":");
                  setDraft({ provider: pName, model: mName || "" });
                }
              }}
              className={cn(
                "bg-void border border-edge text-phosphor-dim hover:text-signal transition-colors font-mono text-[11.25px] uppercase p-1 min-w-0 flex-1 outline-none",
                !draft && "text-glow"
              )}
            >
              {!flatModels.some((o) => o.provider === "z.ai" && o.model === "glm-4.7") && (
                <option value="z.ai:glm-4.7">Z.AI DEFAULT (glm-4.7)</option>
              )}
              {flatModels.map((opt, i) => (
                <option key={i} value={`${opt.provider}:${opt.model || ""}`}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              className={cn("btn !px-2 !py-0.5 !text-[10px]", dirty && "btn--signal")}
              onClick={() => setModel(draft)}
              disabled={!dirty}
              title={dirty ? "Apply selected model" : "No change to apply"}
            >
              APPLY
            </button>
            <button className="btn !px-1.5 !py-0.5 !text-[12px]" onClick={() => void init()} title="refresh">
              ↻
            </button>
            <button className="btn !px-1.5 !py-0.5 !text-[12px]" onClick={onToggleCollapse} title="Collapse sidebar">
              ◀
            </button>
          </div>
        </div>
        {/* Active-model readout — the source of truth for what the next dispatch runs. */}
        <div className="mt-1 flex items-center gap-1.5 text-[10px] text-faint">
          <span className="label !text-[10px]">MODEL ▸</span>
          <span className={cn("mono", dirty ? "text-muted line-through" : "text-signal")}>
            {selectedModel ? `${selectedModel.provider}/${selectedModel.model}` : "—"}
          </span>
          {dirty && (
            <span className="mono text-hazard">→ {draft!.provider}/{draft!.model}</span>
          )}
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-2 border-b border-edge-soft px-3 py-1.5 shrink-0">
        <button
          onClick={() => setTab("sessions")}
          className={cn("hover:text-signal transition-colors uppercase font-bold px-1 py-0.5 text-[13px]", tab === "sessions" && "text-signal border-b border-signal")}
        >
          SESSIONS
        </button>
        <button
          onClick={() => setTab("skills")}
          className={cn("hover:text-signal transition-colors uppercase font-bold px-1 py-0.5 text-[13px]", tab === "skills" && "text-signal border-b border-signal")}
        >
          SKILLS
        </button>
        <button
          onClick={() => setTab("services")}
          className={cn("hover:text-signal transition-colors uppercase font-bold px-1 py-0.5 text-[13px]", tab === "services" && "text-signal border-b border-signal")}
        >
          SERVICES
        </button>
        {tab === "sessions" && <span className="ml-auto mono text-[12px] text-faint">{sessions.length}</span>}
      </div>

      {tab === "sessions" && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {sessions.length === 0 ? (
            <div className="px-3 py-6 text-center">
              <div className="label mb-2">NO SESSIONS</div>
              <p className="text-[13px] leading-relaxed text-faint">
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
                <div
                  key={s.session_id}
                  className={cn(
                    "flex items-center justify-between border-b border-edge-soft transition-colors",
                    active ? "bg-signal/10" : "hover:bg-panel-2",
                  )}
                >
                  <button
                    onClick={() => void select(s.session_id)}
                    className="flex-1 min-w-0 px-3 py-2.5 text-left transition-colors"
                  >
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="flex items-center gap-2">
                        <span className={cn("pip", meta.pip)} />
                        <span
                          className="display text-[12px] font-semibold tracking-[0.14em]"
                          style={{ color: meta.color }}
                        >
                          {meta.label}
                        </span>
                      </span>
                      <span className="mono text-[13px] text-faint" title={s.session_id}>{shortId(s.session_id)}</span>
                    </div>
                    <div className="truncate text-[13px] text-phosphor-dim" title={s.prompt}>{s.prompt}</div>
                    <div className="mt-1 flex items-center gap-3 text-[13px] text-faint">
                      <span>
                        <span className="text-muted">TK</span> {transcripts[s.session_id]?.tokens ?? s.tokens_consumed}
                      </span>
                      {s.error && <span className="text-critical">ERR</span>}
                    </div>
                  </button>
                  <button
                    className="text-faint hover:text-critical p-2.5 text-[12px] shrink-0 font-bold transition-colors"
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleDeleteSession(s.session_id);
                    }}
                    title="Delete session"
                  >
                    ✕
                  </button>
                </div>
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
