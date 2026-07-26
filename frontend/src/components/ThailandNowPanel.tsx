import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { fetchJSON, usePolling } from "../api";
import type { ModuleConfig } from "../store";
import { marked } from "marked"; // NEW: Added marked import

/**
 * THAILAND NOW — monthly content pipeline. Two tabs sharing a desk-driven engine:
 *   WRITERS — bulk-create blank Docs + Trello cards per writer (Paul/Teerin/TIAN).
 *   EVENTS  — two-tier radar: SCOUT (instant keyless) + DEEP RESEARCH (NotebookLM),
 *             → publicity bundle → images → Doc + card with start/due dates.
 */

interface Desk {
  id: string;
  kind: string;
  count: number;
  doc_name: string;
  card_name: string;
  trello_list_name: string;
  labels: string[];
}
interface DesksResp {
  desks: Desk[];
  trello_board_short: string;
  ready: boolean;
}
interface TnEvent {
  title: string;
  url: string;
  start_date?: string;
  end_date?: string;
  signup_deadline?: string;
  location?: string;
  language?: string;
  summary?: string;
  source?: string;
}
interface ScoutResp {
  events: TnEvent[];
  count: number;
  errors: string[];
  window?: { from: string; to: string; weeks: number };
}
interface TnJob {
  id: string;
  kind: string; // "deep-search"
  label: string;
  status: string; // queued | running | done | error | cancelled
  progress: number;
  events: TnEvent[];
  source_urls: string[];
  window: { from: string; to: string; weeks: number } | null;
  notebook: string | null;
  notebook_title: string | null;
  error: string | null;
  logs: string[];
}
interface NbResp {
  notebooks: { id: string; title: string; created_at?: string }[];
}
interface ImagesResp {
  images: { url: string; alt: string }[];
  count: number;
  errors: string[];
  note: string;
}
interface ProvisionItem {
  nn: number;
  doc_name: string;
  doc_url: string;
  card_name: string;
  card_url: string;
}
interface SourcesResp {
  sources: { id: string; title: string; url: string; status: string }[];
}
interface ProvisionResp {
  desk_id: string;
  count: number;
  yyyymm: string;
  items: ProvisionItem[];
}

const CT = { "content-type": "application/json" } as const;

// NEW: Archive types
interface ArchiveSource {
  name: string;
  url: string;
  snippet?: string;
}
interface ArchiveReply {
  answer: string;
  sources: ArchiveSource[];
  mode: "direct" | "synthesized" | "degraded";
  note?: string;
}
interface ArchiveMsg {
  q: string;
  reply: ArchiveReply;
}

// SEO HEALTH report (THAILAND NOW → SEO tab → HEALTH)
interface HealthSource { from: string; from_id?: number; from_title?: string }
interface HealthSuggestion { link: string; title: string }
interface HealthOrphan { id?: number; link: string; title: string; suggested: HealthSuggestion[] }
interface HealthReport {
  post_count: number; page_count: number; event_count: number; other_cpt_count?: number; valid_paths: number;
  broken_internal_links: { from: string; from_id?: number; from_title: string; to: string; href?: string; reason?: string }[];
  internal_manual_check?: { from: string; from_id?: number; from_title: string; to: string; href?: string; reason?: string }[];
  broken_internal_images: { from: string; from_id?: number; from_title: string; src: string; status?: number }[];
  image_manual_check: { from: string; from_id?: number; from_title: string; src: string; status?: number; reason: string }[];
  orphans: HealthOrphan[];
  external_links: string[]; external_imgs: string[];
  broken_external_links: { url: string; status: number; from?: HealthSource[] }[];
  manual_check: { url: string; status?: number; reason: string; from?: HealthSource[] }[];
  external_checked: number; at: string;
}

/** WP admin "edit post" URL derived from a record's own permalink (admin is
 *  same-origin). Returns null when we lack the post id (EDIT hidden then). */
function wpEditUrl(id: number | undefined, link: string): string | null {
  if (!id || !link) return null;
  try { return `${new URL(link).origin}/wp-admin/post.php?post=${id}&action=edit`; }
  catch { return null; }
}

/** Best-effort origin from a permalink, so a bare-path broken-link target
 *  (`/gone/`) resolves to the WP site, not Railjack's own origin. "" on failure. */
function safeOrigin(link: string): string {
  try { return new URL(link).origin; } catch { return ""; }
}

/** ✎ link → open the record's source post in the WP editor (manual fix). Hidden
 *  when no post id is available. S1.4: prominent color for visibility. */
function WpEdit({ id, link }: { id?: number; link: string }) {
  const u = wpEditUrl(id, link);
  return u
    ? <a className="mono font-bold" href={u} target="_blank" rel="noreferrer" title="Edit source in WordPress editor"
        style={{ color: "var(--color-phosphor-dim)", marginLeft: "0.4rem" }}>✎</a>
    : null;
}

async function post<T>(url: string, body: unknown): Promise<{ ok: boolean; data?: T; error?: string }> {
  const res = await fetch(url, { method: "POST", headers: CT, body: JSON.stringify(body) });
  if (!res.ok) {
    const d = await res.json().catch(() => ({ detail: res.statusText }));
    return { ok: false, error: typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail) };
  }
  return { ok: true, data: (await res.json()) as T };
}

/** Current month as YYYYMM (e.g. 202607) — the doc-naming format; recomputed each mount so it
 *  tracks the current month automatically across reloads. */
function currentYyyyMm(): string {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}`;
}
/** Desk kind shown in the UI, uppercased (TIAN → "EMPTY EVENT", else the kind in caps). */
const kindLabel = (d: Desk) => (d.kind === "event" ? "EMPTY EVENT" : d.kind.toUpperCase());

/** Human label for the weeks slider: "4 weeks" under 8, else "≈N months". */
function weeksLabel(n: number): string {
  if (n < 8) return `${n} week${n === 1 ? "" : "s"}`;
  const m = Math.max(1, Math.round(n / 4.33));
  return `≈${m} month${m === 1 ? "" : "s"}`;
}

/** localStorage-backed state — survives module switches + reloads so the user
 *  can walk away and come back to the search results. 8-line helper, used for
 *  events/query/weeks/mode; no external dep (matches the inline pattern in
 *  ComfyPanel/FfmpegPanel, just deduped across N fields). */
function usePersistentState<T>(key: string, initial: T) {
  const [state, setState] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw != null ? (JSON.parse(raw) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(state));
    } catch {
      /* quota / private mode — degrade to in-memory only */
    }
  }, [key, state]);
  return [state, setState] as const;
}

// --- SEO sub-module (HEALTH: read-only link/image/orphan report) ----------------
// Phase 1 = detect-only. Mode-toggle idiom (cf. EventsTab SCOUT/DEEP); HEALTH is
// the first SEO sub-tab. Async scan rides /api/thailandnow/jobs (kind=seo-health)
// exactly like EVENTS DEEP: SCAN → poll → CANCEL → on done fetch /seo/report/{id}.

function SeoTab() {
  const [sub, setSub] = usePersistentState<"health">("tn.seo.tab", "health");
  return (
    <>
      <div className="flex items-center gap-2 mb-2">
        <button className={`btn ${sub === "health" ? "btn--signal" : ""}`} onClick={() => setSub("health")}>
          HEALTH
        </button>
        <span className="mono" style={{ color: "var(--color-muted)" }}>
          read-only link/image/orphan report — Phase 1 (detect); fixes are Phase 2
        </span>
      </div>
      {sub === "health" && <HealthSubTab />}
    </>
  );
}

function healthCopyText(r: HealthReport): string {
  const L: string[] = [];
  L.push(`THAILAND NOW — SEO HEALTH report (${r.at})`);
  L.push(`Scanned ${r.post_count} posts / ${r.page_count} pages / ${r.event_count} events · ${r.external_checked} external links checked`);
  L.push("");
  L.push(`ORPHAN ARTICLES (${r.orphans.length}) — zero inbound internal links:`);
  for (const o of r.orphans) {
    L.push(`- ${o.title} — ${o.link}`);
    for (const s of o.suggested) L.push(`    suggest: ${s.title} — ${s.link}`);
  }
  L.push("");
  L.push(`BROKEN INTERNAL LINKS (${r.broken_internal_links.length}):`);
  for (const b of r.broken_internal_links) L.push(`- ${b.from_title} -> ${b.to}`);
  L.push("");
  L.push(`BROKEN EXTERNAL LINKS (${r.broken_external_links.length}):`);
  for (const b of r.broken_external_links) L.push(`- [${b.status}] ${b.url}`);
  L.push("");
  L.push(`MANUAL CHECK (${r.manual_check.length}) — blocked/timeout, verify by hand:`);
  for (const m of r.manual_check) L.push(`- ${m.url} (${m.reason})`);
  L.push("");
  L.push(`BROKEN IMAGES (${r.broken_internal_images.length}) — HTTP-confirmed missing (404/410):`);
  for (const b of r.broken_internal_images) L.push(`- [${b.status}] ${b.src}  (in: ${b.from_title})`);
  if (r.image_manual_check?.length) {
    L.push("");
    L.push(`IMAGE MANUAL CHECK (${r.image_manual_check.length}) — blocked/timeout, verify by hand:`);
    for (const m of r.image_manual_check) L.push(`- ${m.src}  (in: ${m.from_title}) (${m.reason})`);
  }
  return L.join("\n");
}

function HealthList({ title, count, accent, hint, action, children }: {
  title: string; count: number; accent: string; hint?: string; action?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="border border-edge bg-void p-2">
      <div className="flex items-center justify-between gap-2">
        <div className="label" style={{ color: accent }}>{title} ({count})</div>
        {action}
      </div>
      {hint && <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>{hint}</div>}
      {count > 0 && <div className="scroll-y mt-1">{children}</div>}
    </div>
  );
}

function HealthSubTab() {
  const [report, setReport] = usePersistentState<HealthReport | null>("tn.seo.health.report", null);
  const [err, setErr] = useState<string | null>(null);
  const { data: jobsData, refetch: refetchJobs } = usePolling<{ jobs: TnJob[] }>(
    "/api/thailandnow/jobs", 1500,
  );
  const jobs = jobsData?.jobs ?? [];
  const scanJob = jobs.find((j) => j.kind === "seo-health") ?? null;
  const scanning = !!scanJob && (scanJob.status === "queued" || scanJob.status === "running");

  // Slice 2: Preview / Fix / Dismiss / Bulk states
  const [activePreview, setActivePreview] = useState<{
    key: string;
    postId: number;
    kind: "link" | "image";
    target: string;
    loading: boolean;
    data?: { matches: number; before: string; after: string };
    error?: string;
    applied?: boolean;
  } | null>(null);

  const [dismissed, setDismissed] = usePersistentState<string[]>("tn.seo.dismissed", []);
  const [showDismissed, setShowDismissed] = useState(false);
  const [bulkConfirmSection, setBulkConfirmSection] = useState<string | null>(null);
  const [bulkProgress, setBulkProgress] = useState<string | null>(null);

  const handleStartPreview = async (key: string, postId: number, kind: "link" | "image", target: string) => {
    setActivePreview({ key, postId, kind, target, loading: true });
    const res = await post<{ post_id: number; matches: number; before: string; after: string }>("/api/thailandnow/seo/preview-fix", {
      post_id: postId, kind, target,
    });
    if (res.ok && res.data) {
      setActivePreview({ key, postId, kind, target, loading: false, data: res.data });
    } else {
      setActivePreview({ key, postId, kind, target, loading: false, error: res.error || "Failed to load preview" });
    }
  };

  const handleApplyFix = async () => {
    if (!activePreview || !activePreview.data) return;
    const { postId, kind, target } = activePreview;
    setActivePreview((prev) => prev ? { ...prev, loading: true } : null);
    const res = await post<{ ok: boolean; matches: number }>("/api/thailandnow/seo/apply-fix", {
      post_id: postId, kind, target,
    });
    if (res.ok) {
      setActivePreview((prev) => prev ? { ...prev, loading: false, applied: true } : null);
    } else {
      setActivePreview((prev) => prev ? { ...prev, loading: false, error: res.error || "Failed to apply fix" } : null);
    }
  };

  const startScan = useCallback(async () => {
    setErr(null);
    const r = await post<{ id: string }>("/api/thailandnow/seo/scan", {});
    if (!r.ok) { setErr(r.error ?? "SCAN failed to start"); return; }
    await refetchJobs();
  }, [refetchJobs]);

  // on a seo-health job flipping to done, fetch the report once
  const prevDone = useRef<Set<string>>(new Set());
  useEffect(() => {
    const doneIds = new Set(jobs.filter((j) => j.status === "done").map((j) => j.id));
    const newly = jobs.filter((j) => doneIds.has(j.id) && !prevDone.current.has(j.id));
    prevDone.current = doneIds;
    for (const j of newly) {
      if (j.kind !== "seo-health") continue;
      fetchJSON<HealthReport>(`/api/thailandnow/seo/report/${j.id}`)
        .then(setReport)
        .catch(() => setErr("failed to fetch the report"));
    }
  }, [jobs, setReport]);

  const r = report;

  return (
    <section className="hud hud--bracket reveal reveal-1 p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn btn--signal" disabled={scanning} onClick={startScan}>
          {scanning ? "SCANNING…" : "SCAN"}
        </button>
        {scanJob && (
          <div className="flex items-center gap-2">
            <span className="pip" style={{
              background: scanJob.status === "error" ? "var(--color-critical)"
                : scanJob.status === "done" ? "var(--color-go)" : "var(--color-signal)",
            }} />
            <span className="mono">{scanJob.status.toUpperCase()} · {scanJob.progress}%</span>
            {scanning && (
              <button className="btn btn--crit" onClick={() =>
                fetchJSON(`/api/thailandnow/jobs/${scanJob.id}/cancel`, { method: "POST" }).catch(() => {})
              }>CANCEL</button>
            )}
          </div>
        )}
        {r && (
          <button className="btn" onClick={() => navigator.clipboard.writeText(healthCopyText(r)).catch(() => {})}>
            COPY
          </button>
        )}
        <span className="mono" style={{ color: "var(--color-muted)" }}>
          pulls posts+media via authed WP REST, HTTP-checks links + image candidates — ~1-2 min
        </span>
      </div>

      {err && <div className="mono" style={{ color: "var(--color-critical)" }}>{err}</div>}
      {scanJob?.status === "error" && (
        <div className="mono" style={{ color: "var(--color-critical)" }}>scan failed: {scanJob.error}</div>
      )}

      {r ? (
        <div className="flex flex-col gap-2">
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            {r.post_count} posts / {r.page_count} pages / {r.event_count} events{r.other_cpt_count ? ` / ${r.other_cpt_count} CPTs` : ""} · {r.external_checked} external checked · {r.at}
          </div>

          <HealthList title="ORPHAN ARTICLES" count={r.orphans.length} accent="var(--color-critical)"
            hint="zero inbound internal links — the SEO priority. ✎ opens the editor to un-orphan by hand; link with a suggested target.">
            {r.orphans.map((o) => (
              <div key={o.link} className="text-sm mt-1">
                <a href={o.link} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor)" }}>{o.title || o.link}</a>
                <WpEdit id={o.id} link={o.link} />
                {o.suggested.length > 0 && (
                  <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                    {" — link with: "}
                    {o.suggested.map((s, i) => (
                      <span key={s.link}>
                        {i > 0 && " · "}
                        <a href={s.link} target="_blank" rel="noreferrer" title={s.link}
                          style={{ color: "var(--color-phosphor-dim)" }}>{s.title}</a>
                      </span>
                    ))}
                  </span>
                )}
              </div>
            ))}
          </HealthList>

          <HealthList title="BROKEN INTERNAL LINKS" count={r.broken_internal_links.length} accent="var(--color-hazard)"
            hint="target slug ∉ published set. ✎ opens the source post. Remove strips tag and keeps inner text."
            action={
              r.broken_internal_links.length > 0 && (
                <button
                  className="btn btn--crit text-xs py-0.5 px-1.5"
                  onClick={() => setBulkConfirmSection("internal")}
                >
                  REMOVE ALL ({r.broken_internal_links.length})
                </button>
              )
            }
          >
            {bulkConfirmSection === "internal" && (
              <div className="p-2 border border-critical bg-shade flex flex-col gap-2 my-1">
                <div className="mono text-xs font-bold" style={{ color: "var(--color-critical)" }}>
                  CONFIRM BULK REMOVE ({r.broken_internal_links.length} Broken Internal Links)
                </div>
                <div className="mono text-xs text-muted">
                  Strips all matching broken internal links across source posts, preserving inner text.
                </div>
                {bulkProgress && <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>{bulkProgress}</div>}
                <div className="flex gap-2">
                  <button
                    className="btn btn--crit"
                    disabled={!!bulkProgress}
                    onClick={async () => {
                      setBulkProgress("Applying bulk removal...");
                      const items = r.broken_internal_links
                        .filter((b) => b.from_id)
                        .map((b) => ({ post_id: b.from_id!, kind: "link" as const, target: b.href || b.to }));
                      const res = await post<{ successful: number; total: number }>("/api/thailandnow/seo/apply-fix-bulk", { items });
                      if (res.ok && res.data) {
                        setBulkProgress(`Finished: ${res.data.successful} / ${res.data.total} successful. Re-scan to verify.`);
                      } else {
                        setBulkProgress(`Error: ${res.error || "failed"}`);
                      }
                    }}
                  >
                    CONFIRM BULK REMOVE
                  </button>
                  <button className="btn" onClick={() => { setBulkConfirmSection(null); setBulkProgress(null); }}>CANCEL</button>
                </div>
              </div>
            )}

            {r.broken_internal_links.map((b, i) => {
              const rowKey = `int-${i}-${b.from_id}-${b.to}`;
              return (
                <div key={i} className="text-sm mt-1 flex flex-col gap-0.5">
                  <div>
                    <span style={{ color: "var(--color-phosphor-dim)" }}>{b.from_title || b.from}</span>
                    {" → "}
                    <a href={safeOrigin(b.from) + b.to} target="_blank" rel="noreferrer" style={{ color: "var(--color-critical)" }}>{b.to}</a>
                    <WpEdit id={b.from_id} link={b.from} />
                    {b.from_id && (
                      <button
                        className="btn btn--crit text-xs py-0.5 px-1 ml-2"
                        onClick={() => handleStartPreview(rowKey, b.from_id!, "link", b.href || b.to)}
                      >
                        Remove
                      </button>
                    )}
                    {b.href && b.href !== b.to && b.href !== safeOrigin(b.from) + b.to && (
                      <span className="mono text-xs ml-2" style={{ color: "var(--color-muted)" }}>[raw: {b.href}]</span>
                    )}
                  </div>
                  {b.reason && (
                    <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>{b.reason}</div>
                  )}

                  {activePreview && activePreview.key === rowKey && (
                    <div className="mt-1.5 p-2 border border-edge bg-shade flex flex-col gap-1.5 rounded">
                      <div className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                        FIX PREVIEW — Post #{activePreview.postId}
                      </div>
                      {activePreview.loading && <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>Loading preview...</div>}
                      {activePreview.error && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{activePreview.error}</div>}
                      {activePreview.applied && <div className="mono text-xs font-bold" style={{ color: "var(--color-go)" }}>✓ Removed from WP content! Re-scan to update report.</div>}
                      {!activePreview.loading && !activePreview.applied && activePreview.data && (
                        <>
                          <div className="mono text-xs">Matches found in raw HTML: {activePreview.data.matches}</div>
                          {activePreview.data.matches === 0 ? (
                            <div className="mono text-xs" style={{ color: "var(--color-hazard)" }}>0 matches found in content.raw (may already be removed).</div>
                          ) : (
                            <div className="flex flex-col gap-1">
                              <div className="mono text-xs text-muted">BEFORE:</div>
                              <pre className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap" style={{ background: "rgba(255,0,0,0.1)", border: "1px solid var(--color-critical)" }}>
                                {activePreview.data.before}
                              </pre>
                              <div className="mono text-xs text-muted">AFTER:</div>
                              <pre className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap" style={{ background: "rgba(0,255,0,0.1)", border: "1px solid var(--color-go)" }}>
                                {activePreview.data.after}
                              </pre>
                            </div>
                          )}
                          <div className="flex items-center gap-2 mt-1">
                            <button
                              className="btn btn--crit"
                              disabled={activePreview.loading || activePreview.data.matches === 0}
                              onClick={handleApplyFix}
                            >
                              CONFIRM REMOVE
                            </button>
                            <button className="btn" onClick={() => setActivePreview(null)}>CANCEL</button>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </HealthList>

          {r.internal_manual_check && r.internal_manual_check.length > 0 && (
            <HealthList
              title="INTERNAL MANUAL CHECK"
              count={r.internal_manual_check.filter((b) => showDismissed || !dismissed.includes(`intmc-${b.from}->${b.to}`)).length}
              accent="var(--color-hazard)"
              hint="internal links that returned 403/5xx/timeout — verify by hand. ✕ dismisses from view."
              action={
                dismissed.some((k) => k.startsWith("intmc-")) && (
                  <button className="btn text-xs py-0.5 px-1.5" onClick={() => setShowDismissed((v) => !v)}>
                    {showDismissed ? "Hide Dismissed" : "Show Dismissed"}
                  </button>
                )
              }
            >
              {r.internal_manual_check.map((b, i) => {
                const itemKey = `intmc-${b.from}->${b.to}`;
                const isDismissed = dismissed.includes(itemKey);
                if (isDismissed && !showDismissed) return null;
                return (
                  <div key={i} className="text-sm mt-1 flex flex-col gap-0.5" style={{ opacity: isDismissed ? 0.5 : 1 }}>
                    <div>
                      <span style={{ color: "var(--color-phosphor-dim)" }}>{b.from_title || b.from}</span>
                      {" → "}
                      <a href={safeOrigin(b.from) + b.to} target="_blank" rel="noreferrer" style={{ color: "var(--color-hazard)" }}>{b.to}</a>
                      <WpEdit id={b.from_id} link={b.from} />
                      <button
                        className="mono font-bold text-xs ml-2 hover:text-white"
                        style={{ color: "var(--color-muted)" }}
                        title={isDismissed ? "Restore" : "Dismiss"}
                        onClick={() => setDismissed((prev) => isDismissed ? prev.filter((k) => k !== itemKey) : [...prev, itemKey])}
                      >
                        {isDismissed ? "[Restore]" : "✕"}
                      </button>
                    </div>
                    {b.reason && (
                      <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>{b.reason}</div>
                    )}
                  </div>
                );
              })}
            </HealthList>
          )}

          <HealthList
            title="BROKEN EXTERNAL LINKS"
            count={r.broken_external_links.length}
            accent="var(--color-hazard)"
            hint="HTTP 404/410. ✎ opens the source post. Remove strips tag and keeps inner text."
            action={
              r.broken_external_links.length > 0 && (
                <button
                  className="btn btn--crit text-xs py-0.5 px-1.5"
                  onClick={() => setBulkConfirmSection("external")}
                >
                  REMOVE ALL ({r.broken_external_links.length})
                </button>
              )
            }
          >
            {bulkConfirmSection === "external" && (
              <div className="p-2 border border-critical bg-shade flex flex-col gap-2 my-1">
                <div className="mono text-xs font-bold" style={{ color: "var(--color-critical)" }}>
                  CONFIRM BULK REMOVE ({r.broken_external_links.length} Broken External Links)
                </div>
                <div className="mono text-xs text-muted">
                  Strips all matching broken external links across source posts, preserving inner text.
                </div>
                {bulkProgress && <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>{bulkProgress}</div>}
                <div className="flex gap-2">
                  <button
                    className="btn btn--crit"
                    disabled={!!bulkProgress}
                    onClick={async () => {
                      setBulkProgress("Applying bulk removal...");
                      const items: { post_id: number; kind: "link"; target: string }[] = [];
                      for (const b of r.broken_external_links) {
                        if (b.from && b.from.length > 0) {
                          for (const f of b.from) {
                            if (f.from_id) items.push({ post_id: f.from_id, kind: "link", target: b.url });
                          }
                        }
                      }
                      const res = await post<{ successful: number; total: number }>("/api/thailandnow/seo/apply-fix-bulk", { items });
                      if (res.ok && res.data) {
                        setBulkProgress(`Finished: ${res.data.successful} / ${res.data.total} successful. Re-scan to verify.`);
                      } else {
                        setBulkProgress(`Error: ${res.error || "failed"}`);
                      }
                    }}
                  >
                    CONFIRM BULK REMOVE
                  </button>
                  <button className="btn" onClick={() => { setBulkConfirmSection(null); setBulkProgress(null); }}>CANCEL</button>
                </div>
              </div>
            )}

            {r.broken_external_links.map((b, i) => {
              const src0 = b.from && b.from.length > 0 ? b.from[0] : null;
              const rowKey = `ext-${i}-${src0?.from_id}-${b.url}`;
              return (
                <div key={i} className="text-sm mt-1 flex flex-col gap-0.5">
                  <div>
                    <span className="mono" style={{ color: "var(--color-critical)" }}>[{b.status}]</span>{" "}
                    <a href={b.url} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor)" }}>{b.url}</a>
                    {src0 && <WpEdit id={src0.from_id} link={src0.from} />}
                    {src0?.from_id && (
                      <button
                        className="btn btn--crit text-xs py-0.5 px-1 ml-2"
                        onClick={() => handleStartPreview(rowKey, src0.from_id!, "link", b.url)}
                      >
                        Remove
                      </button>
                    )}
                    {b.from && b.from.length > 1 && (
                      <span className="mono text-xs ml-2" style={{ color: "var(--color-muted)" }}>({b.from.length} posts)</span>
                    )}
                  </div>

                  {activePreview && activePreview.key === rowKey && (
                    <div className="mt-1.5 p-2 border border-edge bg-shade flex flex-col gap-1.5 rounded">
                      <div className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                        FIX PREVIEW — Post #{activePreview.postId}
                      </div>
                      {activePreview.loading && <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>Loading preview...</div>}
                      {activePreview.error && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{activePreview.error}</div>}
                      {activePreview.applied && <div className="mono text-xs font-bold" style={{ color: "var(--color-go)" }}>✓ Removed from WP content! Re-scan to update report.</div>}
                      {!activePreview.loading && !activePreview.applied && activePreview.data && (
                        <>
                          <div className="mono text-xs">Matches found in raw HTML: {activePreview.data.matches}</div>
                          {activePreview.data.matches === 0 ? (
                            <div className="mono text-xs" style={{ color: "var(--color-hazard)" }}>0 matches found in content.raw (may already be removed).</div>
                          ) : (
                            <div className="flex flex-col gap-1">
                              <div className="mono text-xs text-muted">BEFORE:</div>
                              <pre className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap" style={{ background: "rgba(255,0,0,0.1)", border: "1px solid var(--color-critical)" }}>
                                {activePreview.data.before}
                              </pre>
                              <div className="mono text-xs text-muted">AFTER:</div>
                              <pre className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap" style={{ background: "rgba(0,255,0,0.1)", border: "1px solid var(--color-go)" }}>
                                {activePreview.data.after}
                              </pre>
                            </div>
                          )}
                          <div className="flex items-center gap-2 mt-1">
                            <button
                              className="btn btn--crit"
                              disabled={activePreview.loading || activePreview.data.matches === 0}
                              onClick={handleApplyFix}
                            >
                              CONFIRM REMOVE
                            </button>
                            <button className="btn" onClick={() => setActivePreview(null)}>CANCEL</button>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </HealthList>

          <HealthList
            title="MANUAL CHECK"
            count={r.manual_check.filter((m) => showDismissed || !dismissed.includes(`mc-${m.url}`)).length}
            accent="var(--color-hazard)"
            hint="blocked/timeout — verify by hand (NOT confirmed broken). ✕ dismisses from view."
            action={
              dismissed.some((k) => k.startsWith("mc-")) && (
                <button className="btn text-xs py-0.5 px-1.5" onClick={() => setShowDismissed((v) => !v)}>
                  {showDismissed ? "Hide Dismissed" : "Show Dismissed"}
                </button>
              )
            }
          >
            {r.manual_check.map((m, i) => {
              const itemKey = `mc-${m.url}`;
              const isDismissed = dismissed.includes(itemKey);
              if (isDismissed && !showDismissed) return null;
              const src0 = m.from && m.from.length > 0 ? m.from[0] : null;
              return (
                <div key={i} className="text-xs mt-1" style={{ opacity: isDismissed ? 0.5 : 1 }}>
                  <a href={m.url} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor-dim)" }}>{m.url}</a>
                  {" "}
                  <span className="mono" style={{ color: "var(--color-muted)" }}>({m.reason})</span>
                  {src0 && <WpEdit id={src0.from_id} link={src0.from} />}
                  <button
                    className="mono font-bold text-xs ml-2 hover:text-white"
                    style={{ color: "var(--color-muted)" }}
                    title={isDismissed ? "Restore" : "Dismiss"}
                    onClick={() => setDismissed((prev) => isDismissed ? prev.filter((k) => k !== itemKey) : [...prev, itemKey])}
                  >
                    {isDismissed ? "[Restore]" : "✕"}
                  </button>
                  {m.from && m.from.length > 1 && (
                    <span className="mono text-xs ml-2" style={{ color: "var(--color-muted)" }}>({m.from.length} posts)</span>
                  )}
                </div>
              );
            })}
          </HealthList>

          <HealthList
            title="BROKEN IMAGES"
            count={r.broken_internal_images.length}
            accent="var(--color-hazard)"
            hint="HTTP-confirmed missing (404/410). Remove drops img tag entirely."
            action={
              r.broken_internal_images.length > 0 && (
                <button
                  className="btn btn--crit text-xs py-0.5 px-1.5"
                  onClick={() => setBulkConfirmSection("images")}
                >
                  REMOVE ALL ({r.broken_internal_images.length})
                </button>
              )
            }
          >
            {bulkConfirmSection === "images" && (
              <div className="p-2 border border-critical bg-shade flex flex-col gap-2 my-1">
                <div className="mono text-xs font-bold" style={{ color: "var(--color-critical)" }}>
                  CONFIRM BULK REMOVE ({r.broken_internal_images.length} Broken Images)
                </div>
                <div className="mono text-xs text-muted">
                  Drops all matching broken img tags across source posts.
                </div>
                {bulkProgress && <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>{bulkProgress}</div>}
                <div className="flex gap-2">
                  <button
                    className="btn btn--crit"
                    disabled={!!bulkProgress}
                    onClick={async () => {
                      setBulkProgress("Applying bulk removal...");
                      const items = r.broken_internal_images
                        .filter((b) => b.from_id)
                        .map((b) => ({ post_id: b.from_id!, kind: "image" as const, target: b.src }));
                      const res = await post<{ successful: number; total: number }>("/api/thailandnow/seo/apply-fix-bulk", { items });
                      if (res.ok && res.data) {
                        setBulkProgress(`Finished: ${res.data.successful} / ${res.data.total} successful. Re-scan to verify.`);
                      } else {
                        setBulkProgress(`Error: ${res.error || "failed"}`);
                      }
                    }}
                  >
                    CONFIRM BULK REMOVE
                  </button>
                  <button className="btn" onClick={() => { setBulkConfirmSection(null); setBulkProgress(null); }}>CANCEL</button>
                </div>
              </div>
            )}

            {r.broken_internal_images.map((b, i) => {
              const rowKey = `img-${i}-${b.from_id}-${b.src}`;
              return (
                <div key={i} className="text-xs mt-1 flex flex-col gap-0.5">
                  <div>
                    <span className="mono" style={{ color: "var(--color-critical)" }}>[{b.status}]</span>{" "}
                    <a href={b.src} target="_blank" rel="noreferrer" style={{ color: "var(--color-critical)" }}>{b.src}</a>
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}> in {b.from_title}</span>
                    <WpEdit id={b.from_id} link={b.from} />
                    {b.from_id && (
                      <button
                        className="btn btn--crit text-xs py-0.5 px-1 ml-2"
                        onClick={() => handleStartPreview(rowKey, b.from_id!, "image", b.src)}
                      >
                        Remove
                      </button>
                    )}
                  </div>

                  {activePreview && activePreview.key === rowKey && (
                    <div className="mt-1.5 p-2 border border-edge bg-shade flex flex-col gap-1.5 rounded">
                      <div className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                        FIX PREVIEW — Post #{activePreview.postId}
                      </div>
                      {activePreview.loading && <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>Loading preview...</div>}
                      {activePreview.error && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{activePreview.error}</div>}
                      {activePreview.applied && <div className="mono text-xs font-bold" style={{ color: "var(--color-go)" }}>✓ Removed from WP content! Re-scan to update report.</div>}
                      {!activePreview.loading && !activePreview.applied && activePreview.data && (
                        <>
                          <div className="mono text-xs">Matches found in raw HTML: {activePreview.data.matches}</div>
                          {activePreview.data.matches === 0 ? (
                            <div className="mono text-xs" style={{ color: "var(--color-hazard)" }}>0 matches found in content.raw (may already be removed).</div>
                          ) : (
                            <div className="flex flex-col gap-1">
                              <div className="mono text-xs text-muted">BEFORE:</div>
                              <pre className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap" style={{ background: "rgba(255,0,0,0.1)", border: "1px solid var(--color-critical)" }}>
                                {activePreview.data.before}
                              </pre>
                              <div className="mono text-xs text-muted">AFTER:</div>
                              <pre className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap" style={{ background: "rgba(0,255,0,0.1)", border: "1px solid var(--color-go)" }}>
                                {activePreview.data.after}
                              </pre>
                            </div>
                          )}
                          <div className="flex items-center gap-2 mt-1">
                            <button
                              className="btn btn--crit"
                              disabled={activePreview.loading || activePreview.data.matches === 0}
                              onClick={handleApplyFix}
                            >
                              CONFIRM REMOVE
                            </button>
                            <button className="btn" onClick={() => setActivePreview(null)}>CANCEL</button>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </HealthList>

          <HealthList
            title="IMAGE MANUAL CHECK"
            count={(r.image_manual_check ?? []).filter((m) => showDismissed || !dismissed.includes(`imgmc-${m.src}`)).length}
            accent="var(--color-hazard)"
            hint="image probe blocked/timeout — verify by hand (NOT confirmed broken). ✕ dismisses from view."
            action={
              dismissed.some((k) => k.startsWith("imgmc-")) && (
                <button className="btn text-xs py-0.5 px-1.5" onClick={() => setShowDismissed((v) => !v)}>
                  {showDismissed ? "Hide Dismissed" : "Show Dismissed"}
                </button>
              )
            }
          >
            {(r.image_manual_check ?? []).map((m, i) => {
              const itemKey = `imgmc-${m.src}`;
              const isDismissed = dismissed.includes(itemKey);
              if (isDismissed && !showDismissed) return null;
              return (
                <div key={i} className="text-xs mt-1" style={{ opacity: isDismissed ? 0.5 : 1 }}>
                  <a href={m.src} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor-dim)" }}>{m.src}</a>
                  {" "}
                  <span className="mono" style={{ color: "var(--color-muted)" }}>({m.reason})</span>
                  <WpEdit id={m.from_id} link={m.from} />
                  <button
                    className="mono font-bold text-xs ml-2 hover:text-white"
                    style={{ color: "var(--color-muted)" }}
                    title={isDismissed ? "Restore" : "Dismiss"}
                    onClick={() => setDismissed((prev) => isDismissed ? prev.filter((k) => k !== itemKey) : [...prev, itemKey])}
                  >
                    {isDismissed ? "[Restore]" : "✕"}
                  </button>
                </div>
              );
            })}
          </HealthList>
        </div>
      ) : (
        !scanning && <div className="mono" style={{ color: "var(--color-muted)" }}>press SCAN to run a health report</div>
      )}
    </section>
  );
}

export default function ThailandNowPanel({ module: _module }: { module: ModuleConfig }) {
  const [tab, setTab] = useState<"writers" | "events" | "archive" | "seo">("writers");
  const { data, error } = usePolling<DesksResp>("/api/thailandnow/desks", 15000);

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-3">
      <div className="flex shrink-0 items-center gap-2">
        <button
          className={`btn btn--compact ${tab === "writers" ? "btn--signal" : ""}`}
          onClick={() => setTab("writers")}
        >
          WRITERS
        </button>
        <button
          className={`btn btn--compact ${tab === "events" ? "btn--signal" : ""}`}
          onClick={() => setTab("events")}
        >
          EVENTS
        </button>
        <button // NEW: ARCHIVE button
          className={`btn btn--compact ${tab === "archive" ? "btn--signal" : ""}`}
          onClick={() => setTab("archive")}
        >
          ARCHIVE
        </button>
        <button
          className={`btn btn--compact ${tab === "seo" ? "btn--signal" : ""}`}
          onClick={() => setTab("seo")}
        >
          SEO
        </button>
      </div>

      {tab === "writers" && (
        <WritersTab desks={data?.desks ?? []} ready={data?.ready ?? false} loading={!data && !error} error={error} />
      )}
      {tab === "events" && <EventsTab />}
      {tab === "archive" && <ArchiveTab />} {/* NEW: ArchiveTab render */}
      {tab === "seo" && <SeoTab />}
    </div>
  );
}

/* --------------------------------- WRITERS -------------------------------- */

function WritersTab({
  desks,
  ready,
  loading,
  error,
}: {
  desks: Desk[];
  ready: boolean;
  loading: boolean;
  error: string | null;
}) {
  const [deskId, setDeskId] = useState("");
  const [count, setCount] = useState(0);
  const [yyyymm, setYyyymm] = useState(currentYyyyMm);
  const desk = desks.find((d) => d.id === deskId) ?? desks[0];
  const n = count || desk?.count || 1;
  const [busy, setBusy] = useState(false);
  const [items, setItems] = useState<ProvisionItem[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const generate = useCallback(async () => {
    if (!desk) return;
    setBusy(true);
    setErr(null);
    const r = await post<ProvisionResp>("/api/thailandnow/provision", {
      desk_id: desk.id,
      count: n,
      yyyymm: yyyymm.length === 6 ? yyyymm : currentYyyyMm(),
    });
    setBusy(false);
    if (r.ok && r.data) setItems(r.data.items);
    else setErr(r.error ?? "provision failed");
  }, [desk, n, yyyymm]);

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="label mb-2">DESKS</div>
        {loading ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            READING CONFIG
          </div>
        ) : error ? (
          <ErrLine msg={error} />
        ) : !ready || desks.length === 0 ? (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            no desks configured — add options.desks to configs/tawhan.yaml
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {desks.map((d) => (
              <div key={d.id} className="row-in flex items-center gap-2">
                <span className="pip pip--signal" />
                <span className="mono">{d.id.toUpperCase()}</span>
                <span className="label">{kindLabel(d)}</span>
                <span className="mono" style={{ color: "var(--color-muted)" }}>
                  ×{d.count}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {desk && (
        <section className="hud hud--bracket reveal reveal-2 p-3">
          <div className="label mb-2">PROVISION · {desk.id.toUpperCase()}</div>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <label className="label">DESK</label>
            <select
              className="input"
              style={{ width: "auto" }}
              value={desk.id}
              onChange={(e) => {
                setDeskId(e.target.value);
                setCount(0);
              }}
            >
              {desks.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.id.toUpperCase()} ({kindLabel(d)})
                </option>
              ))}
            </select>
            <label className="label">MONTH</label>
            <input
              className="input"
              type="text"
              inputMode="numeric"
              maxLength={6}
              style={{ width: 96 }}
              value={yyyymm}
              onChange={(e) => setYyyymm(e.target.value.replace(/\D/g, "").slice(0, 6))}
            />
            <label className="label">COUNT</label>
            <input
              className="input"
              type="number"
              min={1}
              max={99}
              style={{ width: 72 }}
              value={n}
              onChange={(e) => setCount(Math.max(1, Math.min(99, Number(e.target.value) || 1)))}
            />
            <button className="btn btn--signal" disabled={busy} onClick={generate}>
              {busy ? "GENERATING…" : "GENERATE"}
            </button>
          </div>
          <div className="flex flex-col gap-1">
            <Row label="DOC" value={desk.doc_name} />
            <Row label="CARD" value={desk.card_name} />
            <Row label="LIST" value={desk.trello_list_name} />
          </div>
          {err && (
            <div className="mt-2">
              <ErrLine msg={err} />
            </div>
          )}
        </section>
      )}

      {items.length > 0 && (
        <section className="hud hud--bracket reveal reveal-3 p-3">
          <div className="label mb-2">CREATED · {items.length}</div>
          <div className="flex flex-col gap-1">
            {items.map((it) => (
              <div key={it.nn} className="row-in flex items-center gap-2">
                <span className="pip pip--go" />
                <a className="mono" href={it.doc_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-signal)" }}>
                  {it.doc_name}
                </a>
                <span className="mono" style={{ color: "var(--color-muted)" }}>
                  →
                </span>
                <a className="mono" href={it.card_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor-dim)" }}>
                  {it.card_name}
                </a>
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="row-in flex items-baseline gap-2">
      <span className="label" style={{ minWidth: 44 }}>
        {label}
      </span>
      <span className="mono" style={{ color: "var(--color-phosphor-dim)" }}>
        {value}
      </span>
    </div>
  );
}

/* --------------------------------- EVENTS --------------------------------- */

function EventsTab() {
  // persisted: walk away, come back — results + the form that produced them survive
  const [mode, setMode] = usePersistentState<"scout" | "deep">("tn.mode", "scout");
  const [query, setQuery] = usePersistentState("tn.query", "");
  const [weeks, setWeeks] = usePersistentState("tn.weeks", 4);
  const [events, setEvents] = usePersistentState<TnEvent[]>("tn.events", []);
  const [fetching, setFetching] = useState(false);
  const [busyLabel, setBusyLabel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pickedKey, setPickedKey] = useState<string | null>(null);
  const [selNb, setSelNb] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [notifyPerm, setNotifyPerm] = useState<NotificationPermission>(
    typeof Notification !== "undefined" ? Notification.permission : "denied",
  );

  // DEEP: poll jobs + this mode's notebooks
  const { data: jobsData, refetch: refetchJobs } = usePolling<{ jobs: TnJob[] }>(
    "/api/thailandnow/jobs", 1500,
  );
  const { data: nbData, refetch: refetchNbs } = usePolling<NbResp>(
    "/api/thailandnow/deep/notebooks", 15000,
  );
  const jobs = jobsData?.jobs ?? [];
  const notebooks = nbData?.notebooks ?? [];
  const searchJob = jobs.find((j) => j.kind === "deep-search") ?? null;
  const searching = !!searchJob && (searchJob.status === "queued" || searchJob.status === "running");
  const extracting = fetching && mode === "deep";

  // default-select the newest notebook (first in the sorted list) when none chosen
  useEffect(() => {
    if (mode === "deep" && !selNb && notebooks.length) setSelNb(notebooks[0].id);
  }, [mode, selNb, notebooks]);

  // ask for notification permission once when entering DEEP mode
  useEffect(() => {
    if (mode === "deep" && typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().then(setNotifyPerm);
    }
  }, [mode]);

  // browser-notify when a deep-search job flips to done (and refresh the notebook list)
  const prevDone = useRef<Set<string>>(new Set());
  useEffect(() => {
    const doneIds = new Set(jobs.filter((j) => j.status === "done").map((j) => j.id));
    const newly = jobs.filter((j) => doneIds.has(j.id) && !prevDone.current.has(j.id));
    prevDone.current = doneIds;
    for (const j of newly) {
      if (j.kind !== "deep-search") continue;
      refetchNbs();
      if (notifyPerm === "granted" && typeof Notification !== "undefined") {
        new Notification("Thailand NOW research done", {
          body: `${j.source_urls.length} sources ready — press EXTRACT to pull events.`,
        });
      }
    }
  }, [jobs, notifyPerm, refetchNbs]);

  // dedup key + ascending-by-start-date view (stable for events sharing a date)
  const keyOf = (e: TnEvent) => e.url || e.title;
  const sorted = useMemo(
    () =>
      [...events].sort((a, b) => {
        const sa = a.start_date ?? "";
        const sb = b.start_date ?? "";
        return sa < sb ? -1 : sa > sb ? 1 : 0;
      }),
    [events],
  );

  // SCOUT (instant, sync)
  const runScout = useCallback(async () => {
    setFetching(true);
    setErr(null);
    setBusyLabel(null);
    const r = await post<ScoutResp>("/api/thailandnow/events/scout", { query, weeks });
    setFetching(false);
    if (r.ok && r.data) {
      setEvents((prev) => {
        const map = new Map(prev.map((e) => [keyOf(e), e]));
        for (const e of r.data!.events) map.set(keyOf(e), e);
        return [...map.values()];
      });
    } else {
      setErr(r.error ?? "SCOUT failed");
    }
  }, [query, weeks]);

  // DEEP SEARCH — fire-and-forget (returns job id); browser-notifies on done
  const startDeep = useCallback(async () => {
    setErr(null);
    const r = await post<{ id: string }>("/api/thailandnow/deep/search", { query, weeks });
    if (!r.ok) {
      setErr(r.error ?? "DEEP SEARCH failed to start");
      return;
    }
    await refetchJobs();
  }, [query, weeks, refetchJobs]);

  // DEEP EXTRACT — sync regex extraction on the selected notebook's sources
  const extractDeep = useCallback(async () => {
    if (!selNb) return;
    setFetching(true);
    setErr(null);
    setBusyLabel("EXTRACTING via free regex…");
    const r = await post<ScoutResp>("/api/thailandnow/deep/extract", {
      notebook_id: selNb, query, weeks,
    });
    setFetching(false);
    setBusyLabel(null);
    if (r.ok && r.data) {
      setEvents((prev) => {
        const map = new Map(prev.map((e) => [keyOf(e), e]));
        for (const e of r.data!.events) map.set(keyOf(e), e);
        return [...map.values()];
      });
      if (!r.data.events.length) setErr("no events extracted (sources may be undated or JS-rendered)");
    } else {
      setErr(r.error ?? "DEEP EXTRACT failed");
    }
  }, [selNb, query, weeks]);

  // DEEP seed: tick one source URL → seed a ThickBox event (jina+regex) → open detail
  const pickFromUrl = useCallback(async (url: string) => {
    setSeeding(true);
    setErr(null);
    const r = await post<{ event: TnEvent }>("/api/thailandnow/deep/seed", { url });
    setSeeding(false);
    if (r.ok && r.data?.event) {
      const ev = r.data.event;
      setEvents((prev) => {
        const map = new Map(prev.map((e) => [keyOf(e), e]));
        map.set(keyOf(ev), ev);  // add/replace so the ThickBox lookup finds it
        return [...map.values()];
      });
      setPickedKey(keyOf(ev));  // opens ThickBox
    } else {
      setErr(r.error ?? "seed failed — could not read that URL");
    }
  }, []);

  const picked = pickedKey ? events.find((e) => keyOf(e) === pickedKey) ?? null : null;
  if (picked) {
    return (
      <ThickBox
        key={pickedKey! /* remount per event so editable-date state reseeds */}
        event={picked}
        onBack={() => setPickedKey(null)}
      />
    );
  }

  return (
    <>
      {/* mode toggle */}
      <div className="flex items-center gap-2 mb-2">
        {(["scout", "deep"] as const).map((m) => (
          <button
            key={m}
            className={`btn ${mode === m ? "btn--signal" : ""}`}
            onClick={() => setMode(m)}
          >
            {m === "scout" ? "SCOUT" : "DEEP"}
          </button>
        ))}
        <span className="mono" style={{ color: "var(--color-muted)" }}>
          {mode === "scout"
            ? "instant keyless search"
            : "NotebookLM research + on-demand extract"}
        </span>
      </div>

      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="label mb-2">{mode === "scout" ? "SCOUT" : "DEEP"}</div>
        <div className="mb-2 flex items-center gap-3">
          <input
            type="range"
            min={1}
            max={52}
            step={1}
            value={weeks}
            onChange={(e) => setWeeks(Number(e.target.value))}
            style={{ width: 180 }}
          />
          <span className="mono" style={{ color: "var(--color-signal)" }}>
            {weeksLabel(weeks)}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input"
            style={{ flexGrow: 1, minWidth: 200 }}
            placeholder="optional: business / culture / festival…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {mode === "scout" ? (
            <button className="btn btn--signal" disabled={fetching} onClick={runScout}>
              {fetching && !busyLabel ? "SCANNING…" : "SCOUT"}
            </button>
          ) : (
            <>
              <button className="btn btn--signal" disabled={searching} onClick={startDeep}>
                {searching ? "SEARCHING…" : "SEARCH"}
              </button>
              <button
                className="btn"
                disabled={extracting || !selNb}
                onClick={extractDeep}
              >
                {extracting ? "EXTRACTING…" : "EXTRACT"}
              </button>
            </>
          )}
        </div>

        {mode === "deep" && (
          <div className="mt-2 flex flex-col gap-2">
            {/* notebook browser list */}
            <NbListView notebooks={notebooks} selNb={selNb} setSelNb={setSelNb} onPickUrl={pickFromUrl} busy={seeding} />
            {/* SEARCH job status line */}
            {searchJob && (
              <div className="flex items-center gap-2">
                <span
                  className="pip"
                  style={{
                    background:
                      searchJob.status === "error" ? "var(--color-critical)"
                      : searchJob.status === "done" ? "var(--color-go)"
                      : "var(--color-signal)",
                  }}
                />
                <span className="mono" style={{ minWidth: 110 }}>
                  {searchJob.status.toUpperCase()} · {searchJob.progress}%
                </span>
                {searchJob.status === "done" && (
                  <span className="mono" style={{ color: "var(--color-go)" }}>
                    {searchJob.source_urls.length} sources ready
                  </span>
                )}
                {searching && (
                  <button
                    className="btn btn--crit"
                    onClick={() =>
                      fetchJSON(`/api/thailandnow/jobs/${searchJob.id}/cancel`, { method: "POST" }).catch(() => {})
                    }
                  >
                    CANCEL
                  </button>
                )}
              </div>
            )}
            {/* notification hint */}
            <div className="mono" style={{ color: "var(--color-muted)" }}>
              SEARCH runs NotebookLM in the background
              {notifyPerm === "granted"
                ? " — you'll get a browser notification when done."
                : notifyPerm === "denied"
                ? " — notifications blocked; stay on this tab to see status."
                : " — grant notification permission to be pinged when done."}
              {" "}Then EXTRACT pulls dated events from the notebook (free, no LLM).
            </div>
          </div>
        )}

        {mode === "scout" && (
          <div className="mono mt-1" style={{ color: "var(--color-muted)" }}>
            instant keyless search (Jina + regex, no LLM). Switch to DEEP for NotebookLM research.
          </div>
        )}
      </section>

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">RESULTS{sorted.length ? ` · ${sorted.length}` : ""}</span>
          {sorted.length > 0 && (
            <button className="btn btn--compact btn--crit" onClick={() => setEvents([])}>
              CLEAR
            </button>
          )}
        </div>
        {busyLabel ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            {busyLabel}
          </div>
        ) : fetching ? (
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            SCANNING
          </div>
        ) : err ? (
          <ErrLine msg={err} />
        ) : sorted.length === 0 ? (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            run SCOUT to list upcoming events — click one to draft its publicity bundle
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {sorted.map((e, i) => (
              <button
                key={keyOf(e) || i}
                className="row-in flex items-center gap-2"
                style={{ textAlign: "left", background: "transparent", border: 0, cursor: "pointer" }}
                onClick={() => setPickedKey(keyOf(e))}
              >
                <span className="pip pip--signal" />
                {e.start_date && (
                  <span className="mono" style={{ color: "var(--color-muted)", minWidth: 84 }}>
                    {e.start_date}
                  </span>
                )}
                <span className="mono">{e.title}</span>
                {e.url && (
                  <a
                    href={e.url}
                    target="_blank"
                    rel="noreferrer"
                    className="mono text-sm"
                    style={{ color: "var(--color-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 150 }}
                    onClick={(ev) => ev.stopPropagation()}
                  >
                    ({new URL(e.url).hostname.replace("www.", "")})
                  </a>
                )}
                {e.source && <span className="label">{e.source}</span>}
              </button>
            ))}
          </div>
        )}
      </section>
    </>
  );
}


/* --------------------------------- ARCHIVE -------------------------------- */

function ArchiveTab() {
  const [exchanges, setExchanges] = usePersistentState<ArchiveMsg[]>("tn.archive", []);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [exchanges.length]);

  const ask = useCallback(async () => {
    const q = question.trim();
    if (!q || asking) return;

    setAsking(true);
    setError(null);
    const r = await post<ArchiveReply>("/api/thailandnow/archive/ask", { question: q });
    setAsking(false);

    if (r.ok && r.data) {
      setExchanges((prev) => [...prev, { q, reply: r.data! }]);
      setQuestion("");
    } else {
      setError(r.error || "ask failed");
    }
  }, [question, asking]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      ask();
    }
  }, [ask]);

  return (
    <>
      <section className="hud hud--bracket flex flex-col flex-grow reveal reveal-1 p-3">
        <div className="label mb-2">ARCHIVE CHAT</div>
        <div ref={scrollRef} className="flex flex-col gap-2 overflow-auto flex-grow mb-3">
          {exchanges.length === 0 ? (
            <div className="mono" style={{ color: "var(--color-muted)" }}>
              Ask about past events, news, or general Thailand NOW content.
            </div>
          ) : (
            exchanges.map((ex, i) => (
              <div key={i} className="border border-edge bg-void p-2">
                <div className="flex items-baseline gap-2">
                  <span className="mono" style={{ color: "var(--color-muted)" }}>
                    Q
                  </span>
                  <span className="mono">{ex.q}</span>
                </div>
                <div className="mt-2 prose-md text-sm" dangerouslySetInnerHTML={{ __html: marked.parse(ex.reply.answer) as string }} />
                {ex.reply.sources.length > 0 && (
                  <div className="flex flex-wrap gap-x-2 mt-2">
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>Sources:</span>
                    {ex.reply.sources.map((src, srcIdx) => (
                      <a key={srcIdx} href={src.url} target="_blank" rel="noreferrer" className="mono text-xs" style={{ color: "var(--color-signal)" }}>
                        {src.name}
                      </a>
                    ))}
                  </div>
                )}
                <div className="flex items-center justify-between mt-2">
                  <span className="pip" style={{ background:
                    ex.reply.mode === "direct" ? "var(--color-go)" :
                    ex.reply.mode === "synthesized" ? "var(--color-phosphor)" :
                    "var(--color-critical)"
                  }}></span>
                  <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>{ex.reply.mode.toUpperCase()}</span>
                  <button
                    className="btn btn--compact"
                    onClick={() => navigator.clipboard.writeText(ex.reply.answer).catch(() => {})}
                  >
                   {/* RAW reply text — plain text on the clipboard, no HTML/ANSI artifacts (ARCHIVE plan COPY constraint). */}
                    COPY
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
        {error && <ErrLine msg={error} />}
        <div className="flex shrink-0 items-center gap-2 mt-auto">
          <textarea
            className="input"
            rows={3}
            placeholder="ask about an event…"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={handleKeyDown}
            style={{ flexGrow: 1, resize: "vertical" }}
          />
          <button className="btn btn--signal" disabled={!question.trim() || asking} onClick={ask}>
            {asking ? "ASKING…" : "ASK"}
          </button>
        </div>
      </section>
    </>
  );
}

function ThickBox({ event, onBack }: { event: TnEvent; onBack: () => void }) {
  const [useUrl, setUseUrl] = useState(true);
  const [bundle, setBundle] = useState("");
  const [writing, setWriting] = useState(false);
  const [pubErr, setPubErr] = useState<string | null>(null);
  const [imgs, setImgs] = useState<{ url: string; alt: string }[]>([]);
  const [imgNote, setImgNote] = useState("");
  const [finding, setFinding] = useState(false);
  const [imgErr, setImgErr] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<ProvisionItem | null>(null);
  const [createErr, setCreateErr] = useState<string | null>(null);

  // editable dates — seed from the scouted event; the backend derives the card start/due
  const [startD, setStartD] = useState(event.start_date ?? "");
  const [endD, setEndD] = useState(event.end_date ?? "");
  const [signupD, setSignupD] = useState(event.signup_deadline ?? "");

  const merged = useCallback(
    (): TnEvent => ({
      ...event,
      start_date: startD || undefined,
      end_date: endD || undefined,
      signup_deadline: signupD || undefined,
    }),
    [event, startD, endD, signupD],
  );

  const genBundle = useCallback(async () => {
    setWriting(true);
    setPubErr(null);
    const ev = merged();
    const r = await post<{ bundle: string }>("/api/thailandnow/events/publicize", {
      event: ev,
      urls: useUrl && ev.url ? [ev.url] : [],
    });
    setWriting(false);
    if (r.ok && r.data) setBundle(r.data.bundle);
    else setPubErr(r.error ?? "bundle failed");
  }, [merged, useUrl]);

  const findImages = useCallback(async () => {
    setFinding(true);
    setImgErr(null);
    const r = await post<ImagesResp>("/api/thailandnow/events/images", { url: event.url });
    setFinding(false);
    if (r.ok && r.data) {
      setImgs(r.data.images);
      setImgNote(r.data.note);
    } else setImgErr(r.error ?? "images failed");
  }, [event.url]);

  const create = useCallback(async () => {
    setCreating(true);
    setCreateErr(null);
    const ev = merged();
    const r = await post<ProvisionResp>("/api/thailandnow/events/create", {
      event: ev,
      urls: useUrl && ev.url ? [ev.url] : [],
      bundle_text: bundle,
    });
    setCreating(false);
    if (r.ok && r.data && r.data.items[0]) setCreated(r.data.items[0]);
    else setCreateErr(r.error ?? "create failed");
  }, [merged, useUrl, bundle]);

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">EVENT</span>
          <button className="btn btn--compact" onClick={onBack}>
            ← BACK
          </button>
        </div>
        <div className="mono" style={{ color: "var(--color-phosphor)" }}>
          {event.title}
        </div>
        {event.location && (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            {event.location}
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <label className="label">START</label>
          <input className="input" type="date" value={startD} onChange={(e) => setStartD(e.target.value)} />
          <label className="label">END</label>
          <input className="input" type="date" value={endD} onChange={(e) => setEndD(e.target.value)} />
          <label className="label">SIGNUP</label>
          <input className="input" type="date" value={signupD} onChange={(e) => setSignupD(e.target.value)} />
        </div>
        <div className="mono mt-1" style={{ color: "var(--color-muted)" }}>
          card start/due are set from these (signup deadline → start = due − 7 days)
        </div>
        <label className="mt-2 flex items-center gap-2">
          <input type="checkbox" checked={useUrl} onChange={(e) => setUseUrl(e.target.checked)} />
          <span className="mono" style={{ color: "var(--color-phosphor-dim)" }}>
            use source URL: <a href={event.url} target="_blank" rel="noreferrer" style={{ color: "var(--color-muted)", textDecoration: "underline" }}>{event.url}</a>
          </span>
        </label>
      </section>

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">PUBLICITY BUNDLE</span>
          <button className="btn btn--signal" disabled={writing} onClick={genBundle}>
            {writing ? "DRAFTING…" : "DRAFT COPY"}
          </button>
        </div>
        {pubErr && <ErrLine msg={pubErr} />}
        <textarea
          className="input"
          rows={16}
          placeholder="draft copy (or paste your own) — review before creating the doc"
          value={bundle}
          onChange={(e) => setBundle(e.target.value)}
          style={{ resize: "vertical", fontFamily: "var(--font-mono)" }}
        />
      </section>

      <section className="hud hud--bracket reveal reveal-3 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">IMAGES · {imgs.length}</span>
          <button className="btn btn--compact" disabled={finding} onClick={findImages}>
            {finding ? "FINDING…" : "FIND IMAGES"}
          </button>
        </div>
        {imgErr && <ErrLine msg={imgErr} />}
        {imgs.length > 0 ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(120px,1fr))", gap: 8 }}>
            {imgs.map((im) => (
              <a key={im.url} href={im.url} target="_blank" rel="noreferrer" className="row-in">
                <img
                  src={im.url}
                  alt={im.alt}
                  style={{ width: "100%", height: 80, objectFit: "cover", border: "1px solid var(--color-edge)" }}
                />
              </a>
            ))}
          </div>
        ) : (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            {imgNote || "find images scraped from the event page"}
          </div>
        )}
      </section>

      <section className="hud hud--bracket reveal reveal-4 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">CREATE</span>
          <button className="btn btn--signal" disabled={creating || !bundle.trim()} onClick={create}>
            {creating ? "CREATING…" : "CREATE DOC + CARD"}
          </button>
        </div>
        {createErr && <ErrLine msg={createErr} />}
        {created && (
          <div className="row-in flex flex-wrap items-center gap-2">
            <span className="pip pip--go" />
            <a className="mono" href={created.doc_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-signal)" }}>
              {created.doc_name}
            </a>
            <span className="mono" style={{ color: "var(--color-muted)" }}>
              +
            </span>
            <a className="mono" href={created.card_url} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor-dim)" }}>
              {created.card_name}
            </a>
          </div>
        )}
        {!created && !createErr && (
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            the bundle above becomes the Doc body; the event URL(s) go in the card description
          </div>
        )}
      </section>
    </>
  );
}

function ErrLine({ msg }: { msg: string }) {
  return (
    <div className="row-in flex items-center gap-2">
      <span className="pip pip--crit" />
      <span className="mono" style={{ color: "var(--color-critical)", wordBreak: "break-word" }}>
        {msg}
      </span>
    </div>
  );
}

function NbListView({ notebooks, selNb, setSelNb, onPickUrl, busy }: {
  notebooks: { id: string; title: string; created_at?: string }[];
  selNb: string | null;
  setSelNb: (id: string | null) => void;
  onPickUrl: (url: string) => void;
  busy: boolean;
}) {
  const [expandedNb, setExpandedNb] = useState<string | null>(null);
  // single-select: tick one source URL at a time (ticking another clears the first)
  const [tickedUrl, setTickedUrl] = useState<string | null>(null);
  const { data: sourcesData } = usePolling<SourcesResp>(
    selNb ? `/api/thailandnow/deep/notebooks/${selNb}/sources` : "", 15000,
  );
  const sources = sourcesData?.sources ?? [];

  const copySources = useCallback(() => {
    const readySources = sources.filter((s) => s.status === "ready").map((s) => s.url);
    if (readySources.length > 0) {
      navigator.clipboard.writeText(readySources.join("\n"));
    }
  }, [sources]);

  return (
    <div className="flex flex-col gap-1">
      {notebooks.length === 0 ? (
        <div className="mono" style={{ color: "var(--color-muted)" }}>
          no research notebooks yet — run SEARCH
        </div>
      ) : (
        notebooks.map((nb) => (
          <div key={nb.id} className="row-in flex flex-col items-stretch">
            <button
              className="flex items-center gap-2"
              onClick={() => {
                setSelNb(nb.id);
                setExpandedNb(expandedNb === nb.id ? null : nb.id);
              }}
              style={{ textAlign: "left", background: "transparent", border: 0, cursor: "pointer" }}
            >
              <span className="pip" style={{ background: nb.id === selNb ? "var(--color-signal)" : "var(--color-muted)" }} />
              <span className="mono flex-grow">{(nb.title || nb.id).slice(0, 60)}</span>
              {expandedNb === nb.id ? "▾" : "▸"}
            </button>
            {expandedNb === nb.id && nb.id === selNb && (
              <div className="ml-5 mt-1 border-l border-gray-700 pl-3">
                <div className="flex items-center gap-2 mb-1">
                  <span className="label">SOURCES · tick one → EDIT EVENT</span>
                  <button className="btn btn--compact" onClick={copySources}>copy URLs</button>
                  <button
                    className="btn btn--compact btn--signal"
                    disabled={!tickedUrl || busy}
                    onClick={() => tickedUrl && onPickUrl(tickedUrl)}
                  >
                    {busy ? "OPENING…" : "EDIT EVENT"}
                  </button>
                </div>
                {sources.length === 0 ? (
                  <div className="mono" style={{ color: "var(--color-muted)" }}>no sources found</div>
                ) : (
                  <div className="flex flex-col gap-0.5">
                    {sources.map((src, i) => {
                      const ticked = src.url === tickedUrl;
                      return (
                        <label key={i} className="flex items-center gap-2 mono text-sm" style={{ color: ticked ? "var(--color-signal)" : "var(--color-phosphor-dim)" }}>
                          <input
                            type="checkbox"
                            checked={ticked}
                            onChange={() => setTickedUrl(ticked ? null : src.url)}
                          />
                          <a href={src.url} target="_blank" rel="noreferrer" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {src.title} ({src.status}) {src.url}
                          </a>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
