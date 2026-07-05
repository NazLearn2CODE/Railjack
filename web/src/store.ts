import { create } from "zustand";
import { approve as apiApprove, createSession, createTeam, getHealth, getProviders, getSkills, listSessions, openStream } from "./api";
import type { ContentBlock, Health, ProviderInfo, RoleSpec, Row, ServiceInfo, SessionMeta, SkillInfo, StreamEvent, Transcript, Usage } from "./types";

// Module-level ID for event-log entries — no need to thread a counter through store state.
let _logSeq = 0;
const nextLogId = () => ++_logSeq;

const emptyTranscript = (): Transcript => ({
  rows: [],
  status: "created",
  tokens: 0,
  error: null,
  pendingTools: {},
});

export interface LogEntry {
  id: number;
  label: string;
  tone: "signal" | "hazard" | "go" | "crit" | "muted";
}

interface State {
  sessions: SessionMeta[];
  activeId: string | null;
  transcripts: Record<string, Transcript>;
  log: LogEntry[];
  conn: number; // WebSocket readyState of the active stream
  ws: WebSocket | null;
  health: Health | null; // OS-host posture from GET /api/health
  composing: string;
  mode: "single" | "team"; // team → dispatch a supervisor (POST /api/teams)
  skills: SkillInfo[];
  providers: ProviderInfo[];
  model: { provider: string; model: string } | null;
  embed: ServiceInfo | null;
  init: () => Promise<void>;
  setComposing: (v: string) => void;
  setMode: (m: "single" | "team") => void;
  setModel: (m: { provider: string; model: string } | null) => void;
  setEmbed: (e: ServiceInfo | null) => void;
  dispatch: (prompt: string, systemPrompt?: string, roles?: RoleSpec[]) => Promise<void>;
  select: (id: string) => Promise<void>;
  approve: (approvalId: string, approve: boolean) => Promise<void>;
  approveWorker: (workerId: string, approvalId: string, approve: boolean) => Promise<void>;
}

function rowsFromContent(content: ContentBlock[] | string | undefined): Row[] {
  if (!content) return [];
  if (typeof content === "string") return [{ kind: "text", text: content }];
  const out: Row[] = [];
  for (const b of content) {
    if (b.type === "text") out.push({ kind: "text", text: b.text });
    else if (b.type === "thinking") out.push({ kind: "thinking", text: b.thinking });
    else if (b.type === "tool_use")
      out.push({ kind: "tool_use", id: b.tool_use_id, name: b.name, input: b.input });
  }
  return out;
}

// Full token throughput incl. cache reads/writes — mirrors agent.py _throughput().
// cache_read dominates once the prompt is cached; input+output alone ~100x low.
function tokenTotal(u?: Usage): number {
  if (!u) return 0;
  return (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) +
    (u.cache_creation_input_tokens ?? 0) + (u.output_tokens ?? 0);
}

function applyEvent(t: Transcript, ev: StreamEvent): Transcript {
  const next: Transcript = { ...t, rows: [...t.rows], pendingTools: { ...t.pendingTools } };
  switch (ev.type) {
    case "status":
      if (ev.status) next.status = ev.status;
      if (ev.error) next.error = ev.error;
      break;
    case "message": {
      const rows = rowsFromContent(ev.content);
      if (ev.role === "user" && rows.length) {
        // server-echoed user prompt; client already prepended locally — skip dup
        break;
      }
      next.rows.push(...rows);
      if (ev.usage) next.tokens += tokenTotal(ev.usage);
      break;
    }
    case "result":
      next.rows.push({
        kind: "result",
        text: typeof ev.result === "string" ? ev.result : JSON.stringify(ev.result),
        isError: !!ev.is_error,
      });
      // result.usage is the cumulative-final count (authoritative across backends);
      // set, don't add — assistant-message usage above is incremental only. This is
      // also the sole source under z.ai, where per-message usage is 0.
      const total = tokenTotal(ev.usage);
      if (total > 0) next.tokens = total;
      break;
    case "approval_needed":
      if (ev.approval_id && ev.tool)
        next.pendingTools[ev.approval_id] = { name: ev.tool, input: ev.input };
      break;
    case "worker_event": {
      // A worker's inner activity, forwarded by Team.delegate_many. Nest it into a
      // lane keyed by worker_id, inside the fanout container keyed by fanout_id —
      // the grouped block appears between the delegate_many tool_use and its result.
      type Lane = Extract<Row, { kind: "worker_lane" }>;
      type Fanout = Extract<Row, { kind: "fanout" }>;
      const fid = ev.fanout_id!;
      const wid = ev.worker_id!;
      // find-or-create the fanout container (top-level row)
      const fIdx = next.rows.findIndex((r) => r.kind === "fanout" && r.fanoutId === fid);
      const fanout: Fanout =
        fIdx >= 0
          ? { ...(next.rows[fIdx] as Fanout), lanes: [...(next.rows[fIdx] as Fanout).lanes] }
          : { kind: "fanout", fanoutId: fid, lanes: [] };
      // find-or-create the lane within the fanout
      const lIdx = fanout.lanes.findIndex((l) => l.workerId === wid);
      const lane: Lane =
        lIdx >= 0
          ? { ...fanout.lanes[lIdx], rows: [...fanout.lanes[lIdx].rows] }
          : { kind: "worker_lane", workerId: wid, role: ev.role ?? "?", rows: [], status: "running" };
      const inner = ev.event!;
      if (inner.type === "status" && inner.status) lane.status = inner.status;
      else if (inner.type === "message" && inner.role !== "user")
        lane.rows.push(...rowsFromContent(inner.content));
      else if (inner.type === "result")
        lane.rows.push({
          kind: "result",
          text: typeof inner.result === "string" ? inner.result : JSON.stringify(inner.result),
          isError: !!inner.is_error,
        });
      else if (inner.type === "approval_needed" && inner.approval_id && inner.tool)
        lane.approval = { approvalId: inner.approval_id, tool: inner.tool, input: inner.input };
      if (lIdx >= 0) fanout.lanes[lIdx] = lane;
      else fanout.lanes.push(lane);
      if (fIdx >= 0) next.rows[fIdx] = fanout;
      else next.rows.push(fanout);
      break;
    }
    default:
      break;
  }
  return next;
}

const toneFor = (ev: StreamEvent): LogEntry["tone"] => {
  if (ev.type === "status") {
    if (ev.status === "completed") return "go";
    if (ev.status === "failed") return "crit";
    if (ev.status === "waiting_approval") return "hazard";
    return "signal";
  }
  if (ev.type === "result") return ev.is_error ? "crit" : "go";
  if (ev.type === "rate_limit") return "hazard";
  if (ev.type === "approval_needed") return "hazard";
  if (ev.type === "worker_event") return "signal";
  return "muted";
};

const labelFor = (ev: StreamEvent): string => {
  switch (ev.type) {
    case "status":
      return `STATUS ▸ ${ev.status}`;
    case "message":
      return `MSG ▸ ${ev.role} (${Array.isArray(ev.content) ? ev.content.length : 1} blk)`;
    case "result":
      return `RESULT ▸ ${ev.is_error ? "ERROR" : "OK"}`;
    case "rate_limit":
      return `RATE ▸ ${ev.rate_limit_type}`;
    case "approval_needed":
      return `APPROVAL ▸ ${ev.tool}`;
    case "worker_event":
      return `DELEGATE ▸ ${ev.role}`;
    default:
      return ev.type.toUpperCase();
  }
};

export const useStore = create<State>((set, get) => ({
  sessions: [],
  activeId: null,
  transcripts: {},
  log: [],
  conn: 0,
  ws: null,
  health: null,
  composing: "",
  mode: "single",
  skills: [],
  providers: [],
  model: null,
  embed: null,

  init: async () => {
    // Fetch in parallel; a failure in either leaves the other's slice intact.
    const [s, h, sk, p] = await Promise.allSettled([listSessions(), getHealth(), getSkills(), getProviders()]);
    if (s.status === "fulfilled") set({ sessions: s.value });
    if (h.status === "fulfilled") set({ health: h.value });
    if (sk.status === "fulfilled") set({ skills: sk.value });
    if (p.status === "fulfilled") set({ providers: p.value });
  },

  setComposing: (v) => set({ composing: v }),

  setMode: (m) => set({ mode: m }),

  setModel: (m) => set({ model: m }),

  setEmbed: (e) => set({ embed: e }),

  dispatch: async (prompt, systemPrompt, roles) => {
    const trimmed = prompt.trim();
    if (!trimmed) return;
    set({ composing: "" });
    // Team mode spawns a supervisor (POST /api/teams); otherwise a plain session.
    // Both return a session_id that streams through the identical path below.
    // Empty roles → server hires DEFAULT_ROLES (researcher + coder).
    const modelSel = get().model;
    const provider = modelSel?.provider || null;
    const model = modelSel?.model || null;
    const meta =
      get().mode === "team"
        ? await createTeam(trimmed, roles && roles.length ? roles : undefined, systemPrompt, provider, model)
        : await createSession(trimmed, systemPrompt, provider, model);
    const id = meta.session_id;

    // prepend the user's prompt locally
    const t = emptyTranscript();
    t.rows.push({ kind: "user", text: trimmed });
    t.status = "created";

    // close any prior stream before opening a new one
    const prev = get().ws;
    if (prev) prev.close();

    const ws = openStream(
      id,
      (raw) => {
        const ev = raw as StreamEvent;
        set((s) => {
          const cur = s.transcripts[id] ?? t;
          const applied = applyEvent(cur, ev);
          const entry: LogEntry = { id: nextLogId(), label: labelFor(ev), tone: toneFor(ev) };
          const log = [entry, ...s.log].slice(0, 60);
          const sessions = s.sessions.some((x) => x.session_id === id)
            ? s.sessions.map((x) => (x.session_id === id ? { ...x, status: applied.status } : x))
            : [{ ...meta, status: applied.status }, ...s.sessions];
          return {
            transcripts: { ...s.transcripts, [id]: applied },
            log,
            sessions,
          };
        });
      },
      (readyState) => set({ conn: readyState }),
    );

    set((s) => ({
      activeId: id,
      ws,
      conn: ws.readyState,
      sessions: s.sessions.some((x) => x.session_id === id) ? s.sessions : [meta, ...s.sessions],
      transcripts: { ...s.transcripts, [id]: t },
      log: [
        { id: nextLogId(), label: `DISPATCH ▸ ${id.slice(0, 8)}`, tone: "signal" } satisfies LogEntry,
        ...s.log,
      ].slice(0, 60),
    }));
  },

  select: async (id) => {
    // never re-open the WS for an existing session — that would re-run the agent.
    set({ activeId: id });
    if (get().transcripts[id]) return;
    try {
      const r = await fetch(`/api/sessions/${id}`);
      if (!r.ok) return;
      const data = await r.json();
      let t = emptyTranscript();
      t.rows.push({ kind: "user", text: data.prompt });
      for (const m of data.messages as StreamEvent[]) t = applyEvent(t, m);
      t.status = data.status;
      t.tokens = data.tokens_consumed;
      set((s) => ({ transcripts: { ...s.transcripts, [id]: t } }));
    } catch {
      /* leave empty transcript */
    }
  },

  approve: async (approvalId, approve) => {
    const id = get().activeId;
    if (!id) return;
    await apiApprove(id, approvalId, approve);
    set((s) => {
      const t = s.transcripts[id];
      if (!t) return {};
      const pendingTools = { ...t.pendingTools };
      delete pendingTools[approvalId];
      return { transcripts: { ...s.transcripts, [id]: { ...t, pendingTools } } };
    });
  },

  approveWorker: async (workerId, approvalId, approve) => {
    // A worker's gated tool is actionable through the SAME /approve endpoint,
    // addressed by workerId (registered server-side). Clears the lane card on click.
    await apiApprove(workerId, approvalId, approve);
    set((s) => {
      const id = s.activeId;
      if (!id) return {};
      const t = s.transcripts[id];
      if (!t) return {};
      const rows = t.rows.map((r) => {
        if (r.kind !== "fanout") return r;
        const lanes = r.lanes.map((l) =>
          l.approval?.approvalId === approvalId ? { ...l, approval: undefined } : l,
        );
        return { ...r, lanes };
      });
      return { transcripts: { ...s.transcripts, [id]: { ...t, rows } } };
    });
  },
}));
