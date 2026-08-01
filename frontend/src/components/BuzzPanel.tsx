import { useEffect, useRef, useState } from "react";
import { fetchJSON, usePolling } from "../api";
import type { ModuleConfig } from "../store";

/**
 * BUZZ — sister-to-sister + Naz comms, backed by a self-hosted Buzz relay
 * (Nostr NIP-29 groups) via app/buzz.py's pure-Python NIP-01 client. No
 * Rust/Tauri client anywhere in this path — see [[buzz-agent-workspace]] /
 * [[buzz-module-for-railjack]] in the vault for why.
 *
 * Read as a COMMS LOG, not a messenger-app chat bubble stack: this hub's
 * whole visual language is a telemetry console (ORBITER theme, index.css),
 * so incoming lines read like a radio log — sender callsign + UTC-style
 * timestamp on the left, transmission on the right — rather than the
 * generic left/right bubble pattern every chat app already uses.
 */

interface BuzzState {
  pubkey: string;
  display_name: string;
  relay_url: string;
  channel_id: string | null;
  channel_name: string;
  reachable: boolean;
  error: string | null;
}
interface BuzzMessage {
  id: string;
  pubkey: string;
  content: string;
  created_at: number;
}
interface MessagesResp {
  channel_id: string;
  messages: BuzzMessage[];
}

const shortKey = (pk: string) => pk.slice(0, 8);

// Deterministic hue from a pubkey so each sender gets a stable callsign
// color across the session without a name→color registry to maintain.
function hueFromKey(pk: string): number {
  let h = 0;
  for (let i = 0; i < pk.length; i++) h = (h * 31 + pk.charCodeAt(i)) % 360;
  return h;
}

function timeLabel(unixSecs: number): string {
  const d = new Date(unixSecs * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function BuzzPanel({ module: _module }: { module: ModuleConfig }) {
  const { data: state } = usePolling<BuzzState>("/api/buzz/state", 10_000);
  const { data: msgResp, refetch } = usePolling<MessagesResp>("/api/buzz/messages?limit=200", 3_000);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);
  const prevCount = useRef(0);

  const messages = msgResp?.messages ?? [];

  // Auto-scroll to the newest transmission — but only when it's actually new,
  // so scrolling up to read history doesn't get yanked back down by a poll tick.
  useEffect(() => {
    if (messages.length !== prevCount.current) {
      prevCount.current = messages.length;
      logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
    }
  }, [messages.length]);

  async function send() {
    const content = draft.trim();
    if (!content || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await fetchJSON("/api/buzz/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ content }),
      });
      setDraft("");
      await refetch();
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const pipClass = !state
    ? "pip"
    : state.reachable
      ? "pip pip--go"
      : "pip pip--crit";

  return (
    <div className="flex h-full w-full flex-col gap-2 overflow-hidden p-3">
      {/* Header — channel + own callsign + relay reachability. */}
      <div className="hud hud--bracket reveal reveal-1 flex shrink-0 items-center justify-between gap-3 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className={pipClass} aria-hidden />
          <span className="panel-title">▸ BUZZ</span>
          <span className="label text-muted">
            #{(state?.channel_name ?? "—").toUpperCase()}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {state && (
            <span className="label text-muted" title={state.pubkey}>
              {state.display_name.toUpperCase()} · {shortKey(state.pubkey)}
            </span>
          )}
          <span className="label text-muted">{state?.relay_url ?? ""}</span>
        </div>
      </div>

      {state && !state.reachable && (
        <div className="hud hud--bracket reveal reveal-2 shrink-0 px-3 py-2">
          <span className="label" style={{ color: "var(--color-critical)" }}>
            RELAY UNREACHABLE — {state.error ?? "no connection"}
          </span>
        </div>
      )}

      {/* Comms log. */}
      <div
        ref={logRef}
        className="hud hud--bracket reveal reveal-2 scroll-y flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-3"
        style={{ maxHeight: "none" }}
      >
        {messages.length === 0 && (
          <span className="label text-muted">
            NO TRANSMISSIONS YET{state?.reachable && <span className="caret" />}
          </span>
        )}
        {messages.map((m) => {
          const own = state?.pubkey === m.pubkey;
          const hue = hueFromKey(m.pubkey);
          return (
            <div key={m.id} className="row-in flex gap-2 px-1 py-0.5">
              <span
                className="mono shrink-0 text-xs"
                style={{
                  color: own ? "var(--color-signal)" : `hsl(${hue} 70% 65%)`,
                  minWidth: 64,
                }}
                title={m.pubkey}
              >
                {own ? (state?.display_name ?? "ME").toUpperCase() : shortKey(m.pubkey)}
              </span>
              <span className="mono shrink-0 text-xs text-muted" style={{ minWidth: 72 }}>
                {timeLabel(m.created_at)}
              </span>
              <span className="mono text-sm" style={{ color: "var(--color-phosphor)", whiteSpace: "pre-wrap" }}>
                {m.content}
              </span>
            </div>
          );
        })}
      </div>

      {/* Composer. */}
      <div className="hud hud--bracket reveal reveal-3 flex shrink-0 flex-col gap-1 p-2">
        <div className="flex items-end gap-2">
          <textarea
            className="input"
            style={{ resize: "none", minHeight: 44 }}
            rows={1}
            placeholder="transmit…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={!state?.reachable}
          />
          <button
            className="btn btn--signal"
            onClick={send}
            disabled={!draft.trim() || sending || !state?.reachable}
          >
            SEND{sending && <span className="caret" />}
          </button>
        </div>
        {sendError && (
          <span className="label" style={{ color: "var(--color-critical)" }}>
            {sendError}
          </span>
        )}
      </div>
    </div>
  );
}
