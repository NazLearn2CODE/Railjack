import type { Health, ProviderInfo, RoleSpec, SessionMeta, SkillInfo } from "./types";

export async function getHealth(): Promise<Health> {
  const r = await fetch("/api/health");
  if (!r.ok) throw new Error(`getHealth ${r.status}`);
  return r.json();
}

export async function getSkills(): Promise<SkillInfo[]> {
  const r = await fetch("/api/skills");
  if (!r.ok) throw new Error(`getSkills ${r.status}`);
  return r.json();
}

export async function getProviders(): Promise<ProviderInfo[]> {
  const r = await fetch("/api/providers");
  if (!r.ok) throw new Error(`getProviders ${r.status}`);
  return r.json();
}

export async function refreshModels(): Promise<ProviderInfo[]> {
  // Pull the live model list from the z.ai gateway and return the updated
  // providers in one round-trip (POST /api/models/refresh). Falls back to a
  // plain getProviders() on any failure so the dropdown still renders.
  const r = await fetch("/api/models/refresh", { method: "POST" });
  if (!r.ok) throw new Error(`refreshModels ${r.status}`);
  return r.json();
}

export async function setWorkspaceRoot(root: string): Promise<{ root: string }> {
  const r = await fetch("/api/workspace-root", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ root }),
  });
  if (!r.ok) throw new Error(`setWorkspaceRoot ${r.status}`);
  return r.json();
}

export async function createSession(
  prompt: string,
  systemPrompt?: string,
  provider?: string | null,
  model?: string | null,
): Promise<SessionMeta> {
  const r = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt,
      system_prompt: systemPrompt || null,
      provider: provider || null,
      model: model || null,
    }),
  });
  if (!r.ok) throw new Error(`createSession ${r.status}`);
  return r.json();
}

export async function listSessions(): Promise<SessionMeta[]> {
  const r = await fetch("/api/sessions");
  if (!r.ok) throw new Error(`listSessions ${r.status}`);
  return r.json();
}

export async function createTeam(
  prompt: string,
  roles?: RoleSpec[],
  systemPrompt?: string,
  provider?: string | null,
  model?: string | null,
): Promise<SessionMeta> {
  // Omit `roles` → server hires the default team (researcher + coder). The
  // returned session_id is a supervisor AgentSession, streamed like any other.
  const r = await fetch("/api/teams", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompt,
      system_prompt: systemPrompt || null,
      roles: roles ?? null,
      provider: provider || null,
      model: model || null,
    }),
  });
  if (!r.ok) throw new Error(`createTeam ${r.status}`);
  return r.json();
}

export async function approve(sessionId: string, approvalId: string, approve: boolean): Promise<void> {
  await fetch(`/api/sessions/${sessionId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approval_id: approvalId, approve }),
  });
}

export function openStream(
  sessionId: string,
  onEvent: (e: unknown) => void,
  onState: (s: WebSocket["readyState"]) => void,
): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/sessions/${sessionId}`);
  ws.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data));
    } catch {
      /* ponytail: ignore unparseable frames */
    }
  };
  ws.onopen = () => onState(ws.readyState);
  ws.onclose = () => onState(ws.readyState);
  ws.onerror = () => onState(ws.readyState);
  return ws;
}
