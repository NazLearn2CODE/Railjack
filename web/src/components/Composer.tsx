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
  const providers = useStore((s) => s.providers);
  const selectedModel = useStore((s) => s.model);
  const setModel = useStore((s) => s.setModel);
  const t = useStore((s) => (s.activeId ? s.transcripts[s.activeId] : undefined));
  const [sysOpen, setSysOpen] = useState(false);
  const [sys, setSys] = useState("");
  const [rolesOpen, setRolesOpen] = useState(false);
  const [roles, setRoles] = useState<RoleSpec[]>([]);

  const [addProvOpen, setAddProvOpen] = useState(false);
  const [provName, setProvName] = useState("");
  const [provUrl, setProvUrl] = useState("");
  const [provKey, setProvKey] = useState("");
  const [provModels, setProvModels] = useState("");
  const [provErr, setProvErr] = useState("");

  const initStore = useStore((s) => s.init);

  const handleSaveProvider = async () => {
    if (!provName.trim()) {
      setProvErr("Provider Name is required.");
      return;
    }
    try {
      const modelsList = provModels.split(",")
        .map(m => m.trim())
        .filter(Boolean);
        
      const res = await fetch("/api/providers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: provName.trim(),
          base_url: provUrl.trim() || null,
          api_key: provKey.trim() || null,
          models: modelsList.length ? modelsList : null,
        }),
      });
      
      if (!res.ok) {
        const err = await res.json();
        setProvErr(err.detail || "Failed to save provider.");
        return;
      }
      
      await initStore();
      
      setModel({
        provider: provName.trim(),
        model: modelsList[0] || "",
      });
      
      setProvName("");
      setProvUrl("");
      setProvKey("");
      setProvModels("");
      setProvErr("");
      setAddProvOpen(false);
    } catch (e: any) {
      setProvErr(e.message || "Network error.");
    }
  };

  const team = mode === "team";
  const busy =
    t?.status === "running" ||
    t?.status === "pending_admission" ||
    t?.status === "waiting_approval";

  // A role with an empty name is never hired — drop it on dispatch. Empty roster
  // → server hires DEFAULT_ROLES (researcher + coder).
  const named = roles.filter((r) => r.name.trim());

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

  const selectValue = selectedModel
    ? `${selectedModel.provider}:${selectedModel.model || ""}`
    : "";

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
          className="input mb-2 !py-2 text-[11px]"
          placeholder="system prompt override (optional)…"
          value={sys}
          onChange={(e) => setSys(e.target.value)}
        />
      )}

      {addProvOpen && (
        <div className="mb-2 border border-edge bg-void/60 p-2 text-[10px]">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="label text-[10px]">
              <span className="text-signal">▸</span> ADD CUSTOM PROVIDER
            </span>
            <button className="text-faint hover:text-signal" onClick={() => setAddProvOpen(false)}>
              ✕
            </button>
          </div>
          <div className="grid grid-cols-2 gap-1.5 mb-1.5">
            <input
              className="input !py-1 !text-[10px]"
              placeholder="provider name (e.g. openrouter)"
              value={provName}
              onChange={(e) => setProvName(e.target.value)}
            />
            <input
              className="input !py-1 !text-[10px]"
              placeholder="base url (optional)"
              value={provUrl}
              onChange={(e) => setProvUrl(e.target.value)}
            />
            <input
              className="input !py-1 !text-[10px]"
              type="password"
              placeholder="api key (optional)"
              value={provKey}
              onChange={(e) => setProvKey(e.target.value)}
            />
            <input
              className="input !py-1 !text-[10px]"
              placeholder="models (comma-separated, optional)"
              value={provModels}
              onChange={(e) => setProvModels(e.target.value)}
            />
          </div>
          {provErr && <div className="text-critical text-[9px] mb-1">{provErr}</div>}
          <div className="flex justify-end">
            <button className="btn !px-2 !py-0.5 !text-[10px]" onClick={handleSaveProvider}>
              SAVE PROVIDER
            </button>
          </div>
        </div>
      )}

      {team && rolesOpen && (
        <div className="mb-2 border border-edge bg-void/60 p-2">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="label">
              <span className="text-signal">▸</span> TEAM ROLES
            </span>
            <button className="btn !px-2 !py-1 !text-[11px]" onClick={addRole}>
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
                    className="btn shrink-0 !px-2 !py-1 !text-[11px]"
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
        <span className="display pb-2 text-[9px] text-hazard">▸</span>
        <textarea
          className="input min-h-[46px] resize-none !text-[16px] !py-2.5"
          rows={2}
          placeholder={team ? "DISPATCH A TASK — SUPERVISOR DELEGATES…" : "DISPATCH A PROMPT TO THE AGENT…"}
          value={composing}
          onChange={(e) => setComposing(e.target.value)}
          onKeyDown={onKey}
          disabled={busy}
        />
        <button
          className={cn("btn btn--signal shrink-0 !text-[9px] !py-1.5 !px-3")}
          onClick={send}
          disabled={busy || !composing.trim()}
        >
          {busy ? "·· BUSY" : team ? "DEPLOY" : "DISPATCH"}
        </button>
      </div>
      <div className="mt-1.5 flex items-center justify-between text-[11px] text-faint">
        <span>ENTER ↵ {team ? "DEPLOY TEAM" : "DISPATCH"} · SHIFT+ENTER NEWLINE</span>
        <div className="flex items-center gap-3">
          <button
            className={cn("transition-colors hover:text-signal", team && "text-signal")}
            onClick={() => setMode(team ? "single" : "team")}
          >
            {team ? "● AGENTS" : "○ AGENTS"}
          </button>
          <div className="flex items-center gap-1.5">
            <select
              value={selectValue}
              onChange={(e) => {
                const val = e.target.value;
                if (!val) {
                  setModel(null);
                } else {
                  const [pName, mName] = val.split(":");
                  setModel({ provider: pName, model: mName || "" });
                }
              }}
              className={cn(
                "bg-void border border-edge text-phosphor-dim hover:text-signal transition-colors font-mono text-[9px] uppercase p-1 max-w-[120px] outline-none",
                !selectedModel && "text-glow"
              )}
            >
              <option value="">CLAUDE 3.5 SONNET</option>
              {flatModels.map((opt, i) => (
                <option key={i} value={`${opt.provider}:${opt.model || ""}`}>
                  {opt.label}
                </option>
              ))}
            </select>
            <button
              className="transition-colors hover:text-signal text-[9px] uppercase"
              onClick={() => setAddProvOpen((v) => !v)}
              title="Add custom provider"
            >
              {addProvOpen ? "− PROV" : "+ PROV"}
            </button>
          </div>
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
