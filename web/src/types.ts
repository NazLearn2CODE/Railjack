// Wire shapes streamed by app/main.py WebSocket and REST endpoints.

export type SessionStatus =
  | "created"
  | "pending_admission"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed";

export interface SessionMeta {
  session_id: string;
  prompt: string;
  status: SessionStatus;
  tokens_consumed: number;
  error: string | null;
}

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
}

export interface TextBlock {
  type: "text";
  text: string;
}
export interface ThinkingBlock {
  type: "thinking";
  thinking: string;
}
export interface ToolUseBlock {
  type: "tool_use";
  tool_use_id: string;
  name: string;
  input: unknown;
}
export type ContentBlock = TextBlock | ThinkingBlock | ToolUseBlock;

export interface StreamEvent {
  type: string;
  // message
  role?: string;
  content?: ContentBlock[] | string;
  uuid?: string;
  usage?: Usage;
  // result
  result?: unknown;
  is_error?: boolean;
  // status
  status?: SessionStatus;
  error?: string;
  // rate_limit
  rate_limit_type?: string;
  info?: unknown;
  // approval_needed (milestone 2)
  approval_id?: string;
  tool?: string;
}

// Normalized render rows derived from the stream.
export type Row =
  | { kind: "user"; text: string }
  | { kind: "text"; text: string }
  | { kind: "thinking"; text: string }
  | { kind: "tool_use"; id: string; name: string; input: unknown }
  | { kind: "result"; text: string; isError: boolean };

export interface Transcript {
  rows: Row[];
  status: SessionStatus;
  tokens: number;
  error: string | null;
  pendingTools: Record<string, { name: string; input: unknown }>;
}
