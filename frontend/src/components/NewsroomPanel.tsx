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

interface RadioFolder {
  id?: string;
  name: string;
}

interface RadioCounts {
  weekend: number;
  weekday: number;
  sheet: number;
  planned: number;
  to_create: number;
  skipped: number;
}

interface RadioItem {
  name: string;
  kind?: string;
  id?: string;
  link?: string;
}

interface RadioResponse {
  folder?: RadioFolder;
  dry_run?: boolean;
  counts?: RadioCounts;
  to_create?: RadioItem[];
  created?: RadioItem[];
  skipped?: RadioItem[];
  _fatal?: string;
}

const CT: Record<string, string> = { "content-type": "application/json" };

export default function NewsroomPanel({ module: _module }: { module: ModuleConfig }) {
  const [tab, setTab] = useState<"queue" | "ledger" | "radio">("queue");
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

  // RADIO sub-module state
  const now = new Date();
  const [radioYear, setRadioYear] = useState<number>(now.getFullYear());
  const [radioMonth, setRadioMonth] = useState<number>(now.getMonth() + 1);
  const [radioSheetName, setRadioSheetName] = useState<string>("");
  const [radioPreview, setRadioPreview] = useState<RadioResponse | null>(null);
  const [radioResult, setRadioResult] = useState<RadioResponse | null>(null);
  const [radioLoading, setRadioLoading] = useState<boolean>(false);
  const [radioGenerating, setRadioGenerating] = useState<boolean>(false);

  const handleRadioPreview = async () => {
    setRadioLoading(true);
    setError(null);
    setRadioPreview(null);
    setRadioResult(null);
    try {
      const body: { year: number; month: number; sheet_name?: string } = {
        year: radioYear,
        month: radioMonth,
      };
      if (radioSheetName.trim()) {
        body.sheet_name = radioSheetName.trim();
      }
      const res = await fetch("/api/newsroom/radio/preview", {
        method: "POST",
        headers: CT,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      } else {
        const data: RadioResponse = await res.json();
        setRadioPreview(data);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRadioLoading(false);
    }
  };

  const handleRadioGenerate = async () => {
    if (!radioPreview) return;
    setRadioGenerating(true);
    setError(null);
    try {
      const body: { year: number; month: number; sheet_name?: string } = {
        year: radioYear,
        month: radioMonth,
      };
      if (radioSheetName.trim()) {
        body.sheet_name = radioSheetName.trim();
      }
      const res = await fetch("/api/newsroom/radio/generate", {
        method: "POST",
        headers: CT,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      } else {
        const data: RadioResponse = await res.json();
        setRadioResult(data);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRadioGenerating(false);
    }
  };

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
        <button
          className={`btn btn--compact ${tab === "radio" ? "btn--signal" : ""}`}
          onClick={() => setTab("radio")}
        >
          RADIO
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

      {tab === "radio" && (
        <div className="hud hud--bracket reveal reveal-1 flex min-h-0 flex-1 flex-col gap-3 p-3">
          <div className="flex items-center justify-between">
            <span className="label">RADIO — MONTHLY BATCH GENERATOR</span>
          </div>

          {/* Form controls */}
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 mono text-xs">
              <span className="label">Year</span>
              <input
                type="number"
                className="input mono px-2 py-1 text-xs"
                style={{ width: 80 }}
                value={radioYear}
                onChange={(e) => {
                  setRadioYear(Number(e.target.value));
                  setRadioPreview(null);
                  setRadioResult(null);
                }}
              />
            </label>

            <label className="flex items-center gap-1.5 mono text-xs">
              <span className="label">Month</span>
              <select
                className="mono label"
                style={{
                  background: "var(--color-panel-2)",
                  color: "var(--color-phosphor-dim)",
                  border: "1px solid var(--color-edge)",
                  padding: "4px 6px",
                  fontSize: "11px",
                }}
                value={radioMonth}
                onChange={(e) => {
                  setRadioMonth(Number(e.target.value));
                  setRadioPreview(null);
                  setRadioResult(null);
                }}
              >
                {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                  <option key={m} value={m}>
                    {m < 10 ? `0${m}` : m} — {new Date(2000, m - 1, 1).toLocaleString("en-US", { month: "long" })}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-1 items-center gap-1.5 mono text-xs" style={{ minWidth: 200 }}>
              <span className="label">Sheet Name</span>
              <input
                type="text"
                placeholder="(default: folder name)"
                className="input mono flex-1 px-2 py-1 text-xs"
                value={radioSheetName}
                onChange={(e) => {
                  setRadioSheetName(e.target.value);
                  setRadioPreview(null);
                  setRadioResult(null);
                }}
              />
            </label>

            <div className="ml-auto flex gap-2">
              <button
                className="btn btn--compact"
                onClick={() => void handleRadioPreview()}
                disabled={radioLoading || radioGenerating}
              >
                {radioLoading ? "PREVIEWING…" : "PREVIEW"}
              </button>
              <button
                className="btn btn--compact btn--signal"
                onClick={() => void handleRadioGenerate()}
                disabled={!radioPreview || radioLoading || radioGenerating}
                title={!radioPreview ? "Run PREVIEW first" : "Generate files in Google Drive"}
              >
                {radioGenerating ? "GENERATING…" : "GENERATE"}
              </button>
            </div>
          </div>

          {/* Folder & Counts summary */}
          {(radioPreview || radioResult) && (
            <div className="flex flex-col gap-1.5 border border-edge px-3 py-2 text-xs mono" style={{ background: "var(--color-void)" }}>
              {radioPreview?.folder && (
                <div className="flex items-center gap-2">
                  <span className="label" style={{ color: "var(--color-signal)" }}>TARGET FOLDER:</span>
                  <span style={{ color: "var(--color-phosphor)" }}>{radioPreview.folder.name}</span>
                  {radioPreview.folder.id && (
                    <span style={{ color: "var(--color-muted)" }}>({radioPreview.folder.id})</span>
                  )}
                </div>
              )}

              {(radioResult?.counts || radioPreview?.counts) && (() => {
                const c = radioResult?.counts || radioPreview?.counts!;
                return (
                  <div className="flex items-center gap-2 text-xs">
                    <span className="label">SUMMARY:</span>
                    <span style={{ color: "var(--color-phosphor-dim)" }}>
                      {c.sheet} sheet · {c.weekday} weekday · {c.weekend} weekend · {c.to_create} to create · {c.skipped} skip
                    </span>
                  </div>
                );
              })()}
            </div>
          )}

          {/* Output items list */}
          <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto border border-edge p-2" style={{ background: "var(--color-void)" }}>
            {radioResult ? (
              <>
                <span className="label mb-1" style={{ color: "var(--color-go)" }}>
                  CREATED FILES ({radioResult.created?.length || 0})
                </span>
                {radioResult.created?.map((item, idx) => (
                  <div key={idx} className="mono flex items-center gap-2 border-b border-edge-soft py-1 text-xs">
                    <span className="pip pip--go" />
                    <span className="flex-1 truncate" style={{ color: "var(--color-phosphor)" }}>
                      {item.name}
                    </span>
                    {item.kind && (
                      <span className="label" style={{ fontSize: "10px", color: "var(--color-muted)" }}>
                        {item.kind}
                      </span>
                    )}
                    {item.link ? (
                      <a
                        href={item.link}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: "var(--color-signal)" }}
                      >
                        open ↗
                      </a>
                    ) : null}
                  </div>
                ))}
                {radioResult.skipped && radioResult.skipped.length > 0 && (
                  <div className="mt-2 flex flex-col gap-1">
                    <span className="label" style={{ color: "var(--color-hazard)" }}>
                      SKIPPED FILES ({radioResult.skipped.length})
                    </span>
                    {radioResult.skipped.map((item, idx) => (
                      <div key={idx} className="mono flex items-center gap-2 py-0.5 text-xs">
                        <span className="pip pip--hazard" />
                        <span style={{ color: "var(--color-muted)" }}>{item.name}</span>
                      </div>
                    ))}
                  </div>
                )}
              </>
            ) : radioPreview ? (
              <>
                <span className="label mb-1" style={{ color: "var(--color-signal)" }}>
                  PLAN TO CREATE ({radioPreview.to_create?.length || 0})
                </span>
                {radioPreview.to_create?.map((item, idx) => (
                  <div key={idx} className="mono flex items-center gap-2 border-b border-edge-soft py-1 text-xs">
                    <span className="pip pip--signal" />
                    <span className="flex-1 truncate" style={{ color: "var(--color-phosphor-dim)" }}>
                      {item.name}
                    </span>
                    {item.kind && (
                      <span className="label" style={{ fontSize: "10px", color: "var(--color-muted)" }}>
                        {item.kind}
                      </span>
                    )}
                  </div>
                ))}
              </>
            ) : (
              <div className="flex flex-1 items-center justify-center">
                <span className="label">— Select year & month, then click PREVIEW —</span>
              </div>
            )}
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
