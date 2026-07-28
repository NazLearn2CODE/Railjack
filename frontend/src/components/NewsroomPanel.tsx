import { useCallback, useEffect, useRef, useState } from "react";
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

const renderRewritePreview = (text: string, seoBlock: string) => {
  // Convert **name** → <strong> and -/date/- → <u>; escape everything else.
  // Split on both marker patterns in one pass so they can interleave naturally.
  const htmlText = text
    .split(/(\*\*[^*]+\*\*|-\/[^/]+-\/)/g)
    .map((part) => {
      if (part.startsWith("**") && part.endsWith("**")) {
        return `<strong>${escapeHtml(part.slice(2, -2))}</strong>`;
      }
      if (part.startsWith("-/") && part.endsWith("/-")) {
        return `<u>${escapeHtml(part.slice(2, -2))}</u>`;
      }
      return escapeHtml(part);
    })
    .join("");

  const seoHtml = seoBlock
    ? `<div style="margin-top:16px;border-top:1px solid #445566;padding-top:12px;color:#8fa5b8"><div style="font-weight:600;margin-bottom:8px">AI SEO BLOCK</div><div style="white-space:pre-wrap;font-size:15px">${escapeHtml(seoBlock)}</div></div>`
    : "";

  return (
    `<!doctype html><html><head><meta charset="utf-8"><style>` +
    `body{margin:0;padding:12px;background:#0b0f14;color:#c8d3df;font:17px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-wrap:break-word}` +
    `strong{color:#7dd3fc;font-weight:600}` +
    `u{text-decoration:underline;text-underline-offset:3px;color:#fbbf60}` +
    `</style></head><body>${htmlText}${seoHtml}</body></html>`
  );
};

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

interface NewsDoc {
  id: string;
  name: string;
  link?: string;
  kind: "weekday" | "weekend";
  date: string;
}

interface BrowseFolder {
  id: string;
  name: string;
}

interface NewsBrowseResponse {
  parent: string;
  folders: BrowseFolder[];
  docs: NewsDoc[];
}

interface NewsArticle {
  title: string;
  url: string;
  source: string;
  date: string;
  content: string;
  words: number;
  region?: string;
  rewritten?: boolean;
}

interface NewsReportResponse {
  category: "global" | "business";
  results: NewsArticle[];
  count: number;
  slice_of_life: NewsArticle[];
  mtime: number;
}

interface NewsFillWritten {
  tab: string;
  slot: number;
  title: string;
  region?: string;
}

interface NewsFillSkipped {
  tab: string;
  slot: number;
  reason: string;
}

interface NewsFillApplyResponse {
  doc_id: string;
  category: string;
  written: NewsFillWritten[];
  skipped: NewsFillSkipped[];
  auto?: boolean;
  picked?: number;
}

interface NewsFormatResponse {
  doc_id: string;
  tabs: string[];
  bolded: string[];
  underlined: string[];
  names_skipped: boolean;
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
  const [seo, setSeo] = useState("");
  const [rewriting, setRewriting] = useState(false);

  // RADIO mode toggle: Document Generator (docgen) vs News Fill (newsfill)
  const [radioMode, setRadioMode] = useState<"docgen" | "newsfill">("docgen");

  // RADIO Document Generator state
  const now = new Date();
  const [radioYear, setRadioYear] = useState<number>(now.getFullYear());
  const [radioMonth, setRadioMonth] = useState<number>(now.getMonth() + 1);
  const [radioSheetName, setRadioSheetName] = useState<string>("");
  const [radioPreview, setRadioPreview] = useState<RadioResponse | null>(null);
  const [radioResult, setRadioResult] = useState<RadioResponse | null>(null);
  const [radioLoading, setRadioLoading] = useState<boolean>(false);
  const [radioGenerating, setRadioGenerating] = useState<boolean>(false);

  // RADIO News Fill state — folder browser (RT-2026 → month → day)
  const RRT_PARENT = "1LSw5NwDhwg7PE9pJUO6jKPcd3yFBCOI9";
  const [browseStack, setBrowseStack] = useState<BrowseFolder[]>([
    { id: RRT_PARENT, name: "RT 2026" },
  ]);
  const [browseFolders, setBrowseFolders] = useState<BrowseFolder[]>([]);
  const [browseDocs, setBrowseDocs] = useState<NewsDoc[]>([]);
  const [browseLoading, setBrowseLoading] = useState<boolean>(false);
  const [selectedDoc, setSelectedDoc] = useState<NewsDoc | null>(null);
  const browseInit = useRef<boolean>(false);
  const [newsCategory, setNewsCategory] = useState<"global" | "business">("global");
  const [newsScouting, setNewsScouting] = useState<boolean>(false);
  const [newsReportLoading, setNewsReportLoading] = useState<boolean>(false);
  const [newsReport, setNewsReport] = useState<NewsReportResponse | null>(null);
  const [selectedArticles, setSelectedArticles] = useState<NewsArticle[]>([]);
  const [slicePick, setSlicePick] = useState<NewsArticle | null>(null);
  const [newsApplying, setNewsApplying] = useState<boolean>(false);
  const [newsApplyResult, setNewsApplyResult] = useState<NewsFillApplyResponse | null>(null);
  // Cheap lane: exact-count scout + one-click autopilot (no human curation)
  const [newsCheapScouting, setNewsCheapScouting] = useState<boolean>(false);
  const [newsAutopiloting, setNewsAutopiloting] = useState<boolean>(false);
  // Publication polish — bold names + underline dates on the selected doc.
  const [newsFormatting, setNewsFormatting] = useState<boolean>(false);
  const [newsFormatResult, setNewsFormatResult] = useState<NewsFormatResponse | null>(null);

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

  const loadBrowse = useCallback(async (folderId: string) => {
    setBrowseLoading(true);
    try {
      const data = await fetchJSON<NewsBrowseResponse>(
        `/api/newsroom/radio/news/browse?parent=${encodeURIComponent(folderId)}`,
      );
      setBrowseFolders(Array.isArray(data?.folders) ? data.folders : []);
      setBrowseDocs(Array.isArray(data?.docs) ? data.docs : []);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBrowseLoading(false);
    }
  }, []);

  // Drill into a subfolder: push a breadcrumb, load its children.
  const enterFolder = (f: BrowseFolder) => {
    setBrowseStack((s) => [...s, f]);
    void loadBrowse(f.id);
  };

  // Breadcrumb click: pop back to that level, reload it.
  const jumpToCrumb = (idx: number) => {
    const target = browseStack[idx];
    setBrowseStack((s) => s.slice(0, idx + 1));
    void loadBrowse(target.id);
  };

  useEffect(() => {
    if (tab === "radio" && radioMode === "newsfill" && !browseInit.current) {
      browseInit.current = true;
      void loadBrowse(RRT_PARENT);
    }
  }, [tab, radioMode, loadBrowse, RRT_PARENT]);

  const handleNewsScout = async () => {
    setNewsScouting(true);
    setError(null);
    try {
      await post("/api/terminal/insert", { text: `/radio-news-scout ${newsCategory}` });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setNewsScouting(false);
    }
  };

  const handleNewsConvert = async () => {
    setNewsReportLoading(true);
    setError(null);
    setNewsReport(null);
    setSelectedArticles([]);
    setSlicePick(null);
    setNewsApplyResult(null);
    try {
      const data = await fetchJSON<NewsReportResponse>("/api/newsroom/radio/news/report");
      setNewsReport(data);
      if (data?.category && (data.category === "global" || data.category === "business")) {
        setNewsCategory(data.category);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setNewsReportLoading(false);
    }
  };

  const targetHardCount = selectedDoc?.kind === "weekend" ? (newsCategory === "global" ? 7 : 6) : 10;
  const isWeekdayGlobal = selectedDoc?.kind === "weekday" && newsCategory === "global";

  // Cheap-lane exact-fill counts: N results / M SEA / K slice, keyed on
  // category + kind (matches the backend fill-map — no headroom, AUTOPILOT
  // takes all). A doc with no kind yet is treated as weekday (the default).
  const cheapCounts = (() => {
    if (newsCategory === "global") {
      return selectedDoc?.kind === "weekend" ? { N: 7, M: 2, K: 0 } : { N: 10, M: 3, K: 1 };
    }
    return selectedDoc?.kind === "weekend" ? { N: 6, M: 0, K: 0 } : { N: 10, M: 0, K: 0 };
  })();

  const toggleArticleSelect = (article: NewsArticle) => {
    const existsIndex = selectedArticles.findIndex((a) => a.url === article.url);
    if (existsIndex >= 0) {
      setSelectedArticles(selectedArticles.filter((_, idx) => idx !== existsIndex));
    } else {
      if (selectedArticles.length < targetHardCount) {
        setSelectedArticles([...selectedArticles, article]);
      }
    }
  };

  const isConfirmEnabled =
    Boolean(selectedDoc) &&
    selectedArticles.length === targetHardCount &&
    (!isWeekdayGlobal || slicePick !== null) &&
    !newsApplying;

  const handleNewsApply = async () => {
    if (!selectedDoc || !isConfirmEnabled) return;
    setNewsApplying(true);
    setError(null);
    setNewsApplyResult(null);
    try {
      const body = {
        doc_id: selectedDoc.id,
        kind: selectedDoc.kind,
        category: newsCategory,
        pieces: selectedArticles,
        slice: isWeekdayGlobal ? slicePick : null,
      };
      const res = await fetch("/api/newsroom/radio/news/apply", {
        method: "POST",
        headers: CT,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      } else {
        const data: NewsFillApplyResponse = await res.json();
        setNewsApplyResult(data);
        void runFormat(selectedDoc.id); // auto-chain polish: bold names + underline dates
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setNewsApplying(false);
    }
  };

  // CHEAP SCOUT — inject an exact-count scout invocation into the ttyd pane
  // (same mechanism as SCOUT; the human presses Enter, then AUTOPILOT reads
  // the handoff). Counts come from cheapCounts, so a doc must be picked first.
  const handleCheapScout = async () => {
    if (!selectedDoc) return;
    setNewsCheapScouting(true);
    setError(null);
    try {
      const { N, M, K } = cheapCounts;
      await post("/api/terminal/insert", {
        text: `/radio-news-scout ${newsCategory} --results ${N} --sea ${M} --slice ${K}`,
      });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setNewsCheapScouting(false);
    }
  };

  // AUTOPILOT — one click: backend reads the handoff itself, places every
  // piece deterministically (SEA → GLOBAL slot 1 of each broadcast), fills.
  // No stdin, no pieces payload. Result renders through the APPLY renderer.
  const handleAutopilot = async () => {
    if (!selectedDoc) return;
    setNewsAutopiloting(true);
    setError(null);
    setNewsApplyResult(null);
    try {
      const res = await fetch("/api/newsroom/radio/news/autofill", {
        method: "POST",
        headers: CT,
        body: JSON.stringify({
          doc_id: selectedDoc.id,
          kind: selectedDoc.kind,
          category: newsCategory,
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      } else {
        const data: NewsFillApplyResponse = await res.json();
        setNewsApplyResult(data);
        void runFormat(selectedDoc.id); // auto-chain polish: bold names + underline dates
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setNewsAutopiloting(false);
    }
  };

  // FORMAT — publication polish: bold people names + underline dates across a
  // doc's tabs. Idempotent, so safe to re-run. `runFormat` is the reusable core
  // (also fired automatically after a successful APPLY/AUTOPILOT so a fill is a
  // single one-click flow); `handleFormat` is the standalone-button wrapper.
  const runFormat = async (docId: string) => {
    setNewsFormatting(true);
    setError(null);
    setNewsFormatResult(null);
    try {
      const res = await fetch("/api/newsroom/format/apply", {
        method: "POST",
        headers: CT,
        body: JSON.stringify({ doc_id: docId }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({ detail: res.statusText }));
        setError(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail));
      } else {
        const data: NewsFormatResponse = await res.json();
        setNewsFormatResult(data);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setNewsFormatting(false);
    }
  };

  const handleFormat = () => {
    if (!selectedDoc) return;
    void runFormat(selectedDoc.id);
  };

  // COPY ANTIGRAVITY PROMPT — build the paste-ready prompt with category + kind
  // + counts (from cheapCounts, same source AUTOPILOT trusts), copy to clipboard.
  // Kind defaults to weekday when no doc is picked; counts follow.
  //   curated=false (CHEAP LANE) → EXACT counts, AUTOPILOT places all, no review.
  //   curated=true  (regular lane) → OVER-scout (N+buffer) so CONVERT gives you a
  //     pool to pick from; every piece is still pre-rewritten (rewritten:true), so
  //     your human-picked N place FREE at APPLY (the seam survives CONVERT now).
  const REGULAR_LANE_BUFFER = 5;
  const handleCopyAntigravityPrompt = async (curated = false) => {
    const kind = selectedDoc?.kind ?? "weekday";
    const { N, M, K } = cheapCounts;
    const scoutN = curated ? N + REGULAR_LANE_BUFFER : N;
    const sliceClause = K > 0 ? ` plus ${K} slice-of-life piece` : " and no slice-of-life piece (no AM tab)";
    const countClause = curated
      ? `gather ${scoutN} results (aim for at least ${M} SEA-led)${sliceClause} — a few MORE than the ${N} I need, so I can curate`
      : `gather EXACTLY ${scoutN} results (${M} of them SEA-led)${sliceClause}`;
    const promptText = `Read \`10-knowledge/radio-news-antigravity-handoff.md\` in this vault. Scout recent (dated, ≥190-word) news for category \`${newsCategory}\`, kind \`${kind}\` — ${countClause} — from premium wire/broadcast outlets, rewrite each piece plus the slice-of-life into Editor Ben's ≤250-word broadcast voice (rules in that note), tag SEA pieces, and write the result to \`/tmp/railjack-radio-news/latest.json\` in the exact shape shown — \`"rewritten": true\` on every piece. Do not touch any Google Doc.`;
    try {
      await navigator.clipboard.writeText(promptText);
      setError(null);
      // Brief flash feedback — you might want to render a toast; for now, silent success.
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to copy prompt");
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
    setSeo("");
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
        setSeo(d.seo || "");
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
                        srcDoc={rewriting ? rewriteDoc("processing rewrite…", true) : renderRewritePreview(rewritten, seo)}
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
          {/* Sub-mode navigation header */}
          <div className="flex items-center justify-between border-b border-edge pb-2">
            <div className="flex items-center gap-2">
              <button
                className={`btn btn--compact ${radioMode === "docgen" ? "btn--signal" : ""}`}
                onClick={() => setRadioMode("docgen")}
              >
                Document Generator
              </button>
              <button
                className={`btn btn--compact ${radioMode === "newsfill" ? "btn--signal" : ""}`}
                onClick={() => setRadioMode("newsfill")}
              >
                News Fill
              </button>
            </div>
            <span className="label text-xs">RADIO ▸ {radioMode === "docgen" ? "DOCUMENT GENERATOR" : "NEWS FILL"}</span>
          </div>

          {radioMode === "docgen" ? (
            <>
              {/* Document Generator Form controls */}
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
            </>
          ) : (
            /* News Fill UI */
            <div className="flex min-h-0 flex-1 flex-col gap-3">
              {/* Step 0 & 1 Controls */}
              <div className="flex flex-wrap items-center gap-3 border-b border-edge pb-2">
                {/* Category select */}
                <label className="flex items-center gap-1.5 mono text-xs">
                  <span className="label">Category</span>
                  <select
                    className="mono label"
                    style={{
                      background: "var(--color-panel-2)",
                      color: "var(--color-phosphor)",
                      border: "1px solid var(--color-edge)",
                      padding: "4px 6px",
                      fontSize: "11px",
                    }}
                    value={newsCategory}
                    onChange={(e) => {
                      setNewsCategory(e.target.value as "global" | "business");
                      setSelectedArticles([]);
                      setSlicePick(null);
                    }}
                  >
                    <option value="global">GLOBAL</option>
                    <option value="business">BUSINESS</option>
                  </select>
                </label>

                {/* SCOUT & CONVERT buttons */}
                <div className="flex items-center gap-2 ml-auto">
                  <button
                    className="btn btn--compact"
                    onClick={() => void handleCopyAntigravityPrompt(true)}
                    title={`Copy an Antigravity prompt that OVER-scouts (${cheapCounts.N}+${REGULAR_LANE_BUFFER}) + pre-rewrites, so you curate here and APPLY places your picks free → swap to IDE`}
                  >
                    📋 IDE SCOUT
                  </button>
                  <button
                    className="btn btn--compact"
                    onClick={() => void handleNewsScout()}
                    disabled={newsScouting}
                    title="Type /radio-news-scout command into terminal pane"
                  >
                    {newsScouting ? "SCOUTING…" : "SCOUT"}
                  </button>
                  <button
                    className="btn btn--compact btn--signal"
                    onClick={() => void handleNewsConvert()}
                    disabled={newsReportLoading}
                    title="Fetch and convert latest scout handoff report"
                  >
                    {newsReportLoading ? "LOADING…" : "CONVERT"}
                  </button>
                </div>

                {/* Folder browser: RT 2026 ▸ month ▸ day */}
                <div className="flex w-full flex-col gap-1">
                  {/* Breadcrumb trail */}
                  <div className="flex flex-wrap items-center gap-1 mono text-xs">
                    <span className="label">Target Doc</span>
                    {browseStack.map((crumb, idx) => (
                      <span key={crumb.id} className="flex items-center gap-1">
                        {idx > 0 && <span style={{ color: "var(--color-muted)" }}>▸</span>}
                        <button
                          className="mono"
                          onClick={() => jumpToCrumb(idx)}
                          disabled={idx === browseStack.length - 1}
                          style={{
                            background: "none",
                            border: "none",
                            padding: 0,
                            cursor: idx === browseStack.length - 1 ? "default" : "pointer",
                            color:
                              idx === browseStack.length - 1
                                ? "var(--color-phosphor)"
                                : "var(--color-signal)",
                          }}
                          title={idx === browseStack.length - 1 ? "" : `Back to ${crumb.name}`}
                        >
                          {crumb.name}
                        </button>
                      </span>
                    ))}
                    <button
                      className="btn btn--compact ml-auto"
                      onClick={() => void loadBrowse(browseStack[browseStack.length - 1].id)}
                      disabled={browseLoading}
                      title="Reload this folder"
                    >
                      {browseLoading ? "…" : "⟳"}
                    </button>
                  </div>

                  {/* Folder + doc list for the current level */}
                  <div
                    className="flex flex-col overflow-auto border border-edge"
                    style={{ maxHeight: 150, background: "var(--color-void)" }}
                  >
                    {browseLoading ? (
                      <span className="label mono text-xs p-2">— loading —</span>
                    ) : browseFolders.length === 0 && browseDocs.length === 0 ? (
                      <span className="label mono text-xs p-2">— empty folder —</span>
                    ) : (
                      <>
                        {browseFolders.map((f) => (
                          <button
                            key={f.id}
                            className="mono text-xs flex items-center gap-1.5 px-2 py-1 text-left"
                            onClick={() => enterFolder(f)}
                            style={{
                              background: "none",
                              border: "none",
                              borderBottom: "1px solid var(--color-edge-soft)",
                              color: "var(--color-phosphor-dim)",
                              cursor: "pointer",
                            }}
                          >
                            <span>📁</span>
                            <span className="flex-1 truncate">{f.name}</span>
                            <span style={{ color: "var(--color-muted)" }}>▸</span>
                          </button>
                        ))}
                        {browseDocs.map((doc) => {
                          const active = selectedDoc?.id === doc.id;
                          return (
                            <button
                              key={doc.id}
                              className="mono text-xs flex items-center gap-1.5 px-2 py-1 text-left"
                              onClick={() => {
                                setSelectedDoc(doc);
                                setSelectedArticles([]);
                                setSlicePick(null);
                                setNewsApplyResult(null);
                              }}
                              style={{
                                background: active ? "var(--color-panel-2)" : "none",
                                border: "none",
                                borderBottom: "1px solid var(--color-edge-soft)",
                                color: active ? "var(--color-go)" : "var(--color-phosphor)",
                                cursor: "pointer",
                              }}
                            >
                              <span>{active ? "✓" : "📄"}</span>
                              <span className="flex-1 truncate">{doc.name}</span>
                              <span className="label" style={{ fontSize: "10px", color: "var(--color-muted)" }}>
                                {doc.kind.toUpperCase()}
                              </span>
                            </button>
                          );
                        })}
                      </>
                    )}
                  </div>

                  {/* Current selection echo */}
                  <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                    {selectedDoc
                      ? `Selected: ${selectedDoc.name} [${selectedDoc.kind.toUpperCase()}]`
                      : "— pick a script doc —"}
                  </span>
                </div>

                {/* Cheap lane — exact-count scout → one-click autopilot (no ticking, max token save) */}
                <div
                  className="flex w-full flex-col gap-1.5 border border-edge px-2 py-1.5"
                  style={{ background: "var(--color-void)" }}
                >
                  <div className="flex items-center gap-2">
                    <span className="label" style={{ color: "var(--color-hazard)" }}>
                      CHEAP LANE
                    </span>
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                      no review · max token save
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 mono text-xs">
                    <button
                      className="btn btn--compact"
                      onClick={() => void handleCheapScout()}
                      disabled={!selectedDoc || newsCheapScouting}
                      title={
                        selectedDoc
                          ? `Inject scout for exactly ${cheapCounts.N} results (${cheapCounts.M} SEA, ${cheapCounts.K} slice)`
                          : "Pick a script doc first"
                      }
                    >
                      {newsCheapScouting ? "SCOUTING…" : "CHEAP SCOUT"}
                    </button>
                    <span style={{ color: "var(--color-muted)" }}>
                      {selectedDoc
                        ? `gather exactly ${cheapCounts.N} · ${cheapCounts.M} SEA · ${cheapCounts.K} slice`
                        : "pick a doc"}
                    </span>
                    <span style={{ color: "var(--color-muted)" }}>→</span>
                    <button
                      className="btn btn--compact btn--signal"
                      onClick={() => void handleAutopilot()}
                      disabled={!selectedDoc || newsAutopiloting}
                      title={
                        selectedDoc
                          ? "Convert + place + fill deterministically, no ticking"
                          : "Pick a script doc first"
                      }
                    >
                      {newsAutopiloting ? "AUTOPILOT…" : "AUTOPILOT"}
                    </button>
                    <span style={{ color: "var(--color-muted)" }}>convert + place + fill, no hands</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-2 mono text-xs mt-2">
                    <button
                      className="btn btn--compact"
                      onClick={() => void handleCopyAntigravityPrompt(false)}
                      title="Copy the paste-ready Antigravity prompt (category + kind + exact counts) → swap to IDE"
                    >
                      📋 COPY ANTIGRAVITY PROMPT
                    </button>
                    <span style={{ color: "var(--color-muted)" }}>
                      {newsCategory} · {selectedDoc?.kind ?? "weekday"} · {cheapCounts.N}/{cheapCounts.M} SEA/{cheapCounts.K} slice → IDE
                    </span>
                  </div>
                </div>
              </div>

              {/* Status Header / Target Summary & CONFIRM Button */}
              <div className="flex items-center justify-between px-2 py-1.5 text-xs mono border border-edge gap-2" style={{ background: "var(--color-void)" }}>
                <div className="flex items-center gap-3">
                  <div className="flex items-center gap-1.5">
                    <span className="label" style={{ color: "var(--color-signal)" }}>HARD PICKS:</span>
                    <span style={{ color: selectedArticles.length === targetHardCount ? "var(--color-go)" : "var(--color-hazard)" }}>
                      {selectedArticles.length} / {targetHardCount} selected
                    </span>
                  </div>
                  {isWeekdayGlobal && (
                    <div className="flex items-center gap-1.5">
                      <span className="label" style={{ color: "var(--color-signal)" }}>SLICE OF LIFE:</span>
                      <span style={{ color: slicePick ? "var(--color-go)" : "var(--color-hazard)" }}>
                        {slicePick ? "1 picked" : "0 picked"}
                      </span>
                    </div>
                  )}
                </div>
                <div className="ml-auto flex items-center gap-2">
                  {newsFormatResult && (
                    <span
                      className="mono text-xs"
                      style={{ color: "var(--color-go)" }}
                      title={
                        newsFormatResult.names_skipped
                          ? "gateway down — dates underlined, names skipped"
                          : "publication polish applied"
                      }
                    >
                      ✓ {newsFormatResult.bolded.length} bold · {newsFormatResult.underlined.length} underline
                      {newsFormatResult.names_skipped ? " (names skipped)" : ""}
                    </span>
                  )}
                  <button
                    className="btn btn--compact"
                    onClick={() => void handleFormat()}
                    disabled={!selectedDoc || newsFormatting}
                    title={
                      selectedDoc
                        ? "Bold people names + underline dates on this doc (idempotent)"
                        : "Pick a script doc first"
                    }
                  >
                    {newsFormatting ? "FORMATTING…" : "FORMAT"}
                  </button>
                  <button
                    className="btn btn--compact btn--signal"
                    onClick={() => void handleNewsApply()}
                    disabled={!isConfirmEnabled}
                    title={!isConfirmEnabled ? "Target hard picks and required slice pick must be met" : "Apply selected pieces to target document"}
                  >
                    {newsApplying ? "APPLYING…" : "CONFIRM & APPLY TO DOC"}
                  </button>
                </div>
              </div>

              {/* News Fill Content Area (Checklist & Results) */}
              <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto border border-edge p-2" style={{ background: "var(--color-void)" }}>
                {newsApplyResult ? (
                  <div className="flex flex-col gap-2">
                    <span className="label" style={{ color: "var(--color-go)" }}>
                      ✓ {newsApplyResult.auto ? "AUTOPILOT FILLED" : "APPLIED TO DOC"} —{" "}
                      {newsApplyResult.written.length} SLOTS WRITTEN
                      {typeof newsApplyResult.picked === "number"
                        ? ` (${newsApplyResult.picked} picked)`
                        : ""}
                    </span>
                    <div className="flex flex-col gap-1">
                      {newsApplyResult.written.map((w, idx) => (
                        <div key={idx} className="mono flex items-center gap-2 border-b border-edge-soft py-1 text-xs">
                          <span className="pip pip--go" />
                          <span className="label" style={{ color: "var(--color-signal)" }}>
                            [{w.tab} SLOT {w.slot}]
                          </span>
                          {w.region === "SEA" && (
                            <span
                              className="label"
                              style={{ fontSize: "9px", color: "var(--color-hazard)" }}
                              title="Southeast-Asia lead piece"
                            >
                              SEA
                            </span>
                          )}
                          <span className="flex-1 truncate" style={{ color: "var(--color-phosphor)" }}>
                            {w.title}
                          </span>
                        </div>
                      ))}
                    </div>
                    {newsApplyResult.skipped.length > 0 && (
                      <div className="mt-2 flex flex-col gap-1">
                        <span className="label" style={{ color: "var(--color-hazard)" }}>
                          SKIPPED SLOTS ({newsApplyResult.skipped.length})
                        </span>
                        {newsApplyResult.skipped.map((s, idx) => (
                          <div key={idx} className="mono flex items-center gap-2 py-0.5 text-xs">
                            <span className="pip pip--hazard" />
                            <span className="label" style={{ color: "var(--color-muted)" }}>
                              [{s.tab} SLOT {s.slot}]
                            </span>
                            <span style={{ color: "var(--color-muted)" }}>{s.reason}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : newsReport ? (
                  <div className="flex flex-col gap-3">
                    {/* Hard News Checklist */}
                    <div>
                      <div className="flex items-center justify-between mb-1">
                        <span className="label" style={{ color: "var(--color-signal)" }}>
                          SCOUTED ARTICLES ({newsReport.results?.length || 0}) — PICK {targetHardCount}
                        </span>
                        {selectedArticles.length > 0 && (
                          <button
                            className="btn btn--compact"
                            onClick={() => setSelectedArticles([])}
                            style={{ fontSize: "10px", padding: "1px 6px" }}
                          >
                            Clear Picks
                          </button>
                        )}
                      </div>
                      <div className="flex flex-col gap-1">
                        {newsReport.results?.map((article) => {
                          const selectIndex = selectedArticles.findIndex((a) => a.url === article.url);
                          const isSelected = selectIndex >= 0;
                          return (
                            <div
                              key={article.url}
                              onClick={() => toggleArticleSelect(article)}
                              className="mono flex cursor-pointer items-start gap-2 border border-edge px-2 py-1.5 transition-colors"
                              style={{
                                background: isSelected
                                  ? "color-mix(in srgb, var(--color-signal) 12%, var(--color-panel-2))"
                                  : "var(--color-panel-2)",
                                borderColor: isSelected ? "var(--color-signal)" : "var(--color-edge)",
                              }}
                            >
                              <span
                                className="label mt-0.5"
                                style={{
                                  color: isSelected ? "var(--color-signal)" : "var(--color-muted)",
                                  minWidth: 28,
                                }}
                              >
                                {isSelected ? `[#${selectIndex + 1}]` : "[  ]"}
                              </span>
                              <div className="flex flex-1 flex-col gap-0.5 min-w-0">
                                <div className="flex items-center justify-between gap-2">
                                  <span
                                    className="font-semibold text-xs truncate flex items-center gap-1.5"
                                    style={{
                                      color: isSelected ? "var(--color-phosphor)" : "var(--color-phosphor-dim)",
                                    }}
                                  >
                                    {(article.region || "").toUpperCase() === "SEA" && (
                                      <span className="label" style={{ fontSize: "9px", color: "var(--color-hazard)" }} title="Southeast-Asia lead piece">
                                        SEA
                                      </span>
                                    )}
                                    {article.rewritten && (
                                      <span className="label" style={{ fontSize: "9px", color: "var(--color-go)" }} title="Pre-rewritten by Antigravity — APPLY places it free (no local rewrite)">
                                        ✎READY
                                      </span>
                                    )}
                                    <span className="truncate">{article.title}</span>
                                  </span>
                                  <span className="label text-xs shrink-0" style={{ color: "var(--color-muted)", fontSize: "10px" }}>
                                    {article.source} · {article.words}w · {article.date}
                                  </span>
                                </div>
                                {article.content && (
                                  <span className="text-xs truncate" style={{ color: "var(--color-muted)", fontSize: "11px" }}>
                                    {article.content.slice(0, 140)}…
                                  </span>
                                )}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    {/* Slice of Life Section (weekday global only) */}
                    {isWeekdayGlobal && newsReport.slice_of_life && newsReport.slice_of_life.length > 0 && (
                      <div className="border-t border-edge pt-2">
                        <span className="label mb-1 block" style={{ color: "var(--color-hazard)" }}>
                          SLICE OF LIFE (PICK EXACTLY 1)
                        </span>
                        <div className="flex flex-col gap-1">
                          {newsReport.slice_of_life.map((slice) => {
                            const isPicked = slicePick?.url === slice.url;
                            return (
                              <div
                                key={slice.url}
                                onClick={() => setSlicePick(isPicked ? null : slice)}
                                className="mono flex cursor-pointer items-start gap-2 border border-edge px-2 py-1.5"
                                style={{
                                  background: isPicked
                                    ? "color-mix(in srgb, var(--color-hazard) 15%, var(--color-panel-2))"
                                    : "var(--color-panel-2)",
                                  borderColor: isPicked ? "var(--color-hazard)" : "var(--color-edge)",
                                }}
                              >
                                <input
                                  type="radio"
                                  name="slice_pick"
                                  checked={isPicked}
                                  onChange={() => setSlicePick(slice)}
                                  className="mt-1"
                                />
                                <div className="flex flex-1 flex-col gap-0.5 min-w-0">
                                  <div className="flex items-center justify-between gap-2">
                                    <span
                                      className="font-semibold text-xs truncate"
                                      style={{
                                        color: isPicked ? "var(--color-phosphor)" : "var(--color-phosphor-dim)",
                                      }}
                                    >
                                      {slice.title}
                                    </span>
                                    <span className="label text-xs shrink-0" style={{ color: "var(--color-muted)", fontSize: "10px" }}>
                                      {slice.source} · {slice.words}w
                                    </span>
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-1 flex-col items-center justify-center gap-2">
                    <span className="label">— Click SCOUT to run terminal scout or CONVERT to load report —</span>
                  </div>
                )}
              </div>
            </div>
          )}
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
