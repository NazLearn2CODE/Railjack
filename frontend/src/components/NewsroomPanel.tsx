import { useCallback, useEffect, useState } from "react";
import { fetchJSON } from "../api";
import type { ModuleConfig } from "../store";

/**
 * NEWSROOM panel — thin HUD over the newsroom skill CLI (queue.py +
 * nl_append.py). Queue list with author filter, story detail pane with an
 * editable script area, SEND TO NL (append + auto-mark), and a ledger tab.
 * Port of Somatic's NewsroomPanel (18ef2ff) into home Railjack's idiom.
 *
 * REWRITE runs the Script-box text through the backend Rules-Gem pass
 * (/api/newsroom/rewrite, source-only — keeps Thai names/titles in the
 * original script) and renders the finished two-layer script in an embedded
 * iframe; "⇐ load into Script" pulls it into the editable box before SEND TO NL.
 */

// Wrap the rewritten script in a self-contained dark HTML doc for the iframe.
const escapeHtml = (s: string) =>
  s.replace(/[&<>]/g, (c) => (({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }) as Record<string, string>)[c]);
const rewriteDoc = (body: string, muted = false) =>
  `<!doctype html><html><head><meta charset="utf-8"><style>` +
  `body{margin:0;padding:12px;background:#0b0f14;` +
  `color:${muted ? "#5f7285" : "#c8d3df"};` +
  `font:17px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;` +
  `white-space:pre-wrap;word-wrap:break-word}</style></head><body>${escapeHtml(body)}</body></html>`;

interface Story {
  id: string;
  date: string;
  author: string;
  footage_code: string;
  rundown: string;
  title: string;
  shortdesc: string;
  detail: string;
  link: string;
}
interface Queue {
  date: string;
  author: string;
  count: number;
  articles: Story[];
}
interface LedgerEntry {
  id: string;
  status: string;
  doc_id: string;
  processed_at: string;
}

const CT: Record<string, string> = { "content-type": "application/json" };

export default function NewsroomPanel({ module: _module }: { module: ModuleConfig }) {
  const [tab, setTab] = useState<"queue" | "ledger">("queue");
  const [queue, setQueue] = useState<Queue | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [selected, setSelected] = useState<Story | null>(null);
  const [sendText, setSendText] = useState("");
  const [author, setAuthor] = useState("Chompatsorn");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rewritten, setRewritten] = useState("");
  const [rewriting, setRewriting] = useState(false);

  const refreshQueue = useCallback(() => {
    setLoading(true);
    fetchJSON<Queue>(`/api/newsroom/queue?author=${encodeURIComponent(author)}`)
      .then((q) => {
        setQueue(q);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [author]);
  const refreshLedger = useCallback(() => {
    fetchJSON<LedgerEntry[]>("/api/newsroom/ledger")
      .then((d) => setLedger(Array.isArray(d) ? d : []))
      .catch(() => setLedger([]));
  }, []);

  useEffect(() => {
    refreshQueue();
    refreshLedger();
  }, [refreshQueue, refreshLedger]);

  // POST helper surfacing the backend's HTTPException detail (fetchJSON only
  // reports the status code, and the stderr tail is the useful part here).
  const post = async (url: string, body: unknown): Promise<boolean> => {
    setError(null);
    const res = await fetch(url, { method: "POST", headers: CT, body: JSON.stringify(body) });
    if (!res.ok) {
      const d = await res.json().catch(() => ({ detail: res.statusText }));
      setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
    }
    return res.ok;
  };

  const selectStory = (s: Story) => {
    setSelected(s);
    setSendText(s.detail);
  };

  const sendToNL = async () => {
    if (!selected || !sendText.trim()) return;
    setSending(true);
    // Append first; only a successful drop stamps the ledger (the dedup).
    if (await post("/api/newsroom/append", { today: true, text: sendText })) {
      await post("/api/newsroom/mark", { ids: [selected.id] });
      setSelected(null);
      setSendText("");
      refreshQueue();
      refreshLedger();
    }
    setSending(false);
  };

  // REWRITE: POST the Script-box text to the backend Rules-Gem pass (which
  // rides the OmniRoute gateway), then render the finished two-layer script in
  // the iframe. Inlined (not via `post`) because we need the response body.
  const rewrite = async () => {
    if (!selected || !sendText.trim()) return;
    setRewriting(true);
    setRewritten("");
    setError(null);
    try {
      const res = await fetch("/api/newsroom/rewrite", {
        method: "POST",
        headers: CT,
        body: JSON.stringify({ text: sendText }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      } else {
        const d = await res.json().catch(() => ({}));
        setRewritten(d.rewritten || "");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRewriting(false);
    }
  };

  return (
    <div className="flex h-full w-full flex-col gap-2 overflow-auto p-3">
      {/* Tab toggle + author filter */}
      <div className="flex items-center gap-2">
        <button
          className={`btn btn--compact ${tab === "queue" ? "btn--signal" : ""}`}
          onClick={() => setTab("queue")}
        >
          QUEUE
        </button>
        <button
          className={`btn btn--compact ${tab === "ledger" ? "btn--signal" : ""}`}
          onClick={() => {
            setTab("ledger");
            refreshLedger();
          }}
        >
          LEDGER
        </button>
        {tab === "queue" && (
          <>
            <button className="btn btn--compact" onClick={refreshQueue} disabled={loading}>
              {loading ? "…" : "⟳"}
            </button>
            <select
              className="mono label ml-auto"
              style={{
                background: "var(--color-panel-2)",
                color: "var(--color-phosphor-dim)",
                border: "1px solid var(--color-edge)",
                padding: "3px 5px",
                fontSize: "11px",
              }}
              value={author}
              onChange={(e) => setAuthor(e.target.value)}
            >
              <option value="Chompatsorn">Chompatsorn</option>
              <option value="all">All reporters</option>
            </select>
          </>
        )}
      </div>

      {tab === "queue" && (
        <div className="hud hud--bracket reveal reveal-1 flex min-h-0 flex-1 flex-col gap-2 p-3">
          <span className="label">
            QUEUE {queue ? `— ${queue.date} — ${queue.count} stories` : ""}
          </span>
          <div className="flex min-h-0 flex-1 gap-2 overflow-hidden">
            {/* Story list */}
            <div className="flex flex-col gap-1 overflow-auto" style={{ width: "38%", minWidth: 180 }}>
              {queue?.articles.map((s) => (
                <div
                  key={s.id}
                  onClick={() => selectStory(s)}
                  className="row-in mono flex cursor-pointer items-center gap-2 border border-edge px-2 py-1"
                  style={{
                    background: "var(--color-void)",
                    color:
                      selected?.id === s.id ? "var(--color-signal)" : "var(--color-phosphor-dim)",
                  }}
                >
                  <span className="flex-1 truncate text-xs">
                    {s.footage_code ? (
                      <span style={{ color: "var(--color-go)" }}>{s.footage_code} </span>
                    ) : null}
                    {s.title}
                  </span>
                </div>
              ))}
              {queue && queue.count === 0 && <span className="label px-1 py-1">— no stories —</span>}
              {!queue && !error && <span className="label px-1 py-1">loading…</span>}
            </div>

            {/* Detail pane */}
            <div className="flex min-w-0 flex-1 flex-col gap-2 overflow-auto">
              {selected ? (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="label" style={{ color: "var(--color-go)" }}>
                      {selected.footage_code || "—"}
                    </span>
                    <span className="label" style={{ color: "var(--color-muted)" }}>
                      {selected.rundown}
                    </span>
                    <span className="label ml-auto" style={{ color: "var(--color-muted)" }}>
                      {selected.author} · {selected.date}
                    </span>
                  </div>
                  <span
                    className="mono"
                    style={{ color: "var(--color-phosphor)", fontWeight: 600 }}
                  >
                    {selected.title}
                  </span>
                  {selected.shortdesc && (
                    <span className="mono text-xs" style={{ color: "var(--color-phosphor-dim)" }}>
                      {selected.shortdesc}
                    </span>
                  )}
                  {selected.link && (
                    <a
                      href={selected.link}
                      target="_blank"
                      rel="noreferrer"
                      className="mono text-xs"
                      style={{ color: "var(--color-signal)" }}
                    >
                      source ↗
                    </a>
                  )}

                  {/* Editable script area — paste the rewritten text here before sending */}
                  <label className="flex min-h-0 flex-1 flex-col gap-1">
                    <span className="label">Script (edit before sending)</span>
                    <textarea
                      value={sendText}
                      onChange={(e) => setSendText(e.target.value)}
                      className="input mono flex-1"
                      style={{ resize: "none", fontSize: "1.0625rem" }}
                    />
                  </label>

                  <div className="flex gap-2">
                    <button
                      className="btn"
                      onClick={() => void rewrite()}
                      disabled={rewriting || !sendText.trim()}
                      title="Run the Script-box text through the Rules Gem (source-only) → two-layer script"
                    >
                      {rewriting ? "REWRITING…" : "REWRITE"}
                    </button>
                    <button
                      className="btn btn--signal"
                      onClick={() => void sendToNL()}
                      disabled={sending || !sendText.trim()}
                    >
                      {sending ? "SENDING…" : "SEND TO NL"}
                    </button>
                  </div>

                  {/* Rewritten article — Rules-Gem output, rendered in an iframe */}
                  {(rewriting || rewritten) && (
                    <div className="flex min-h-0 flex-col gap-1" style={{ minHeight: 160 }}>
                      <div className="flex items-center gap-2">
                        <span className="label">
                          Rewritten article{rewriting ? " — processing…" : ""}
                        </span>
                        {rewritten && (
                          <button
                            className="btn btn--compact ml-auto"
                            style={{ padding: "2px 8px" }}
                            onClick={() => setSendText(rewritten)}
                          >
                            ⇐ load into Script
                          </button>
                        )}
                      </div>
                      <iframe
                        title="rewritten article"
                        srcDoc={rewriting ? rewriteDoc("processing rewrite…", true) : rewriteDoc(rewritten)}
                        style={{
                          width: "100%",
                          minHeight: 140,
                          border: "1px solid var(--color-edge)",
                          background: "var(--color-void)",
                        }}
                      />
                    </div>
                  )}
                </>
              ) : (
                <div className="flex flex-1 items-center justify-center">
                  <span className="label">select a story</span>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {tab === "ledger" && (
        <div className="hud hud--bracket reveal reveal-1 flex min-h-0 flex-1 flex-col gap-2 p-3">
          <span className="label">LEDGER — {ledger.length} processed</span>
          <div className="flex-1 overflow-auto">
            {ledger.length === 0 && <span className="label">— nothing processed yet —</span>}
            {ledger.map((e, i) => (
              <div key={i} className="mono flex items-center gap-2 py-0.5 text-xs">
                <span className="pip pip--go" />
                <span style={{ color: "var(--color-phosphor-dim)" }}>{e.id}</span>
                <span style={{ color: "var(--color-muted)" }}>{e.doc_id || "—"}</span>
                <span className="ml-auto" style={{ color: "var(--color-muted)" }}>
                  {e.processed_at}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {error && (
        <div className="mono px-2 text-xs" style={{ color: "var(--color-critical)" }}>
          ✗ {error}
        </div>
      )}
    </div>
  );
}
