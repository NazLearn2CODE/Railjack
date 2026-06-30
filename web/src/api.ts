import type { SessionMeta } from "./types";

export async function createSession(prompt: string, systemPrompt?: string): Promise<SessionMeta> {
  const r = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, system_prompt: systemPrompt || null }),
  });
  if (!r.ok) throw new Error(`createSession ${r.status}`);
  return r.json();
}

export async function listSessions(): Promise<SessionMeta[]> {
  const r = await fetch("/api/sessions");
  if (!r.ok) throw new Error(`listSessions ${r.status}`);
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
