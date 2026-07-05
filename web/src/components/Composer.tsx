import { useState, type KeyboardEvent } from "react";
import { useStore } from "../store";
import { cn } from "../util";
import type { RoleSpec } from "../types";

export default function Composer() {
  const composing = useStore((s) => s.composing);
  const setComposing = useStore((s) => s.setComposing);
  const dispatch = useStore((s) => s.dispatch);
  const mode = useStore((s) => s.mode);
  const setMode = useStore((s) => s.setMode);
  const t = useStore((s) => (s.activeId ? s.transcripts[s.activeId] : undefined));
  const [sysOpen, setSysOpen] = useState(false);
  const [sys, setSys] = useState("");
  const [rolesOpen, setRolesOpen] = useState(false);
  const [roles, setRoles] = useState<RoleSpec[]>([]);

  const team = mode === "team";
  const busy =
    t?.status === "running" ||
    t?.status === "pending_admission" ||
    t?.status === "waiting_approval";

  // A role with an empty name is never hired — drop it on dispatch. Empty roster
  // → server hires DEFAULT_ROLES (researcher + coder).
  const named = roles.filter((r) => r.name.trim());

  const send = () => {
    if (busy || !composing.trim()) return;
    void dispatch(composing, sys || undefined, named.length ? named : undefined);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const addRole = () => setRoles((rs) => [...rs, { name: "", system_prompt: "" }]);
  const updateRole = (i: number, patch: Partial<RoleSpec>) =>
    setRoles((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const removeRole = (i: number) => setRoles((rs) => rs.filter((_, j) => j !== i));

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

      {team && rolesOpen && (
        <div className="mb-2 border border-edge bg-void/60 p-2">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="label">
              <span className="text-signal">▸</span> TEAM ROLES
            </span>
            <button className="btn !px-2 !py-1 !text-[9px]" onClick={addRole}>
              + ADD ROLE
            </button>
          </div>
          {roles.length === 0 ? (
            <p className="text-[10px] leading-relaxed text-faint">
              DEFAULT TEAM ▸ researcher + coder. Add a role to customize the roster
              (custom roles replace the defaults entirely).
            </p>
          ) : (
            <div className="flex flex-col gap-1.5">
              {/* ponytail: index keys — focus may jump when removing a middle role;
                  fine for a small roster; stable ids if it grows. */}
              {roles.map((r, i) => (
                <div key={i} className="flex items-center gap-1.5">
                  <input
                    className="input !w-[120px] shrink-0 !py-1 !text-[11px]"
                    placeholder="role name"
                    value={r.name}
                    onChange={(e) => updateRole(i, { name: e.target.value })}
                  />
                  <input
                    className="input min-w-0 flex-1 !py-1 !text-[11px]"
                    placeholder="system prompt — what this specialist does…"
                    value={r.system_prompt}
                    onChange={(e) => updateRole(i, { system_prompt: e.target.value })}
                  />
                  <button
                    className="btn shrink-0 !px-2 !py-1 !text-[9px]"
                    onClick={() => removeRole(i)}
                    title="remove role"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
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
          {(() => {
            const providers = useStore((s) => s.providers);
            const selectedModel = useStore((s) => s.model);
            const setModel = useStore((s) => s.setModel);

            const modelOptions: ({ provider: string; model: string | null } | null)[] = [null];
            for (const p of providers) {
              if (p.models.length === 0) {
                modelOptions.push({ provider: p.name, model: null });
              } else {
                for (const m of p.models) {
                  modelOptions.push({ provider: p.name, model: m });
                }
              }
            }

            if (modelOptions.length <= 1) return null;

            const cycleModel = () => {
              const currentIndex = modelOptions.findIndex(
                (opt) =>
                  (opt === null && selectedModel === null) ||
                  (opt !== null &&
                    selectedModel !== null &&
                    opt.provider === selectedModel.provider &&
                    opt.model === selectedModel.model)
              );
              const nextIndex = (currentIndex + 1) % modelOptions.length;
              const nextOpt = modelOptions[nextIndex];
              if (nextOpt === null) {
                setModel(null);
              } else {
                setModel({ provider: nextOpt.provider, model: nextOpt.model || "" });
              }
            };

            const getModelLabel = () => {
              if (!selectedModel) return "DEFAULT";
              return selectedModel.model ? `${selectedModel.provider}/${selectedModel.model}` : selectedModel.provider;
            };

            return (
              <button
                className={cn("transition-colors hover:text-signal uppercase", selectedModel && "text-signal")}
                onClick={cycleModel}
                title="Cycle model/provider"
              >
                MDL ▸ {getModelLabel()}
              </button>
            );
          })()}
          {team && (
            <button
              className={cn("transition-colors hover:text-signal", rolesOpen && "text-signal")}
              onClick={() => setRolesOpen((v) => !v)}
            >
              {rolesOpen ? "− ROLES" : named.length ? `▾ ROLES · ${named.length}` : "+ ROLES"}
            </button>
          )}
          <button className="transition-colors hover:text-signal" onClick={() => setSysOpen((v) => !v)}>
            {sysOpen ? "− HIDE SYS" : "+ SYS PROMPT"}
          </button>
        </div>
      </div>
    </div>
  );
}
