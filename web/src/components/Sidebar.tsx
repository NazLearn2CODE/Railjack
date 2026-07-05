import { useState } from "react";
import { useStore } from "../store";
import { cn, shortId, statusMeta } from "../util";
import SkillsList from "./SkillsList";
import ServicesList from "./ServicesList";

// Common model names for the dropdown suggestions when providers list none.
const KNOWN_MODELS = [
  "claude-sonnet-5",
  "claude-opus-4-8",
  "claude-haiku-4-5-20251001",
  "claude-fable-5",
];

export default function Sidebar({ onDismiss }: { onDismiss: () => void }) {
  const sessions = useStore((s) => s.sessions);
  const activeId = useStore((s) => s.activeId);
  const transcripts = useStore((s) => s.transcripts);
  const select = useStore((s) => s.select);
  const init = useStore((s) => s.init);
  const providers = useStore((s) => s.providers);
  const selectedModel = useStore((s) => s.model);
  const setModel = useStore((s) => s.setModel);
  const [tab, setTab] = useState<"sessions" | "skills" | "services">("sessions");

  // Build flat list of all model suggestions for the datalist.
  const suggestions = providers.flatMap((p) =>
    p.models.length ? p.models.map((m) => `${p.name}/${m}`) : [],
  );
  const allOptions = suggestions.length > 0 ? suggestions : KNOWN_MODELS;

  const currentDisplay = selectedModel
    ? selectedModel.model
      ? `${selectedModel.provider}/${selectedModel.model}`
      : selectedModel.provider
    : "DEFAULT";

  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [customInput, setCustomInput] = useState("");

  const selectOption = (val: string) => {
    if (!val.trim()) { setModel(null); }
    else if (val.includes("/")) {
      const [provider, model] = val.split("/", 2);
      setModel({ provider, model });
    } else {
      setModel({ provider: providers[0]?.name ?? "default", model: val });
    }
    setDropdownOpen(false);
    setCustomInput("");
  };

  return (
    <aside className="reveal reveal-2 hud hud--bracket m-2 mr-1 flex min-h-0 flex-col">
      {/* Model selector — always visible at top */}
      <div className="flex items-center justify-between border-b border-edge px-3 py-2 gap-2">
        <div className="flex items-center gap-2 min-w-0 flex-1 relative">
          <span className="pip pip--signal shrink-0" />
          {dropdownOpen ? (
            <div className="flex items-center gap-1 min-w-0 flex-1">
              <input
                autoFocus
                list="model-options"
                className="input !py-0.5 !text-[12px] !px-1.5 min-w-0 flex-1"
                placeholder="type or select model…"
                value={customInput}
                onChange={(e) => setCustomInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") { selectOption(customInput); } if (e.key === "Escape") setDropdownOpen(false); }}
              />
              <datalist id="model-options">
                <option value="DEFAULT" />
                {allOptions.map((m) => <option key={m} value={m} />)}
              </datalist>
              <button className="btn !px-1.5 !py-0.5 !text-[12px] shrink-0" onClick={() => selectOption(customInput)}>SET</button>
            </div>
          ) : (
            <button
              className="flex items-center gap-1.5 hover:text-signal transition-colors min-w-0"
              onClick={() => { setDropdownOpen(true); setCustomInput(""); }}
              title="Click to change model"
            >
              <span className="display text-[12px] font-bold tracking-[0.14em] text-phosphor-dim truncate">{currentDisplay}</span>
              <span className="label text-[10px] text-faint shrink-0">▾</span>
            </button>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <button className="btn !px-1.5 !py-0.5 !text-[12px]" onClick={() => void init()} title="refresh">
            ↻
          </button>
          <button className="btn !px-1.5 !py-0.5 !text-[12px] hover:text-critical" onClick={onDismiss} title="dismiss">
            ✕
          </button>
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-2 border-b border-edge-soft px-3 py-1.5">
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
