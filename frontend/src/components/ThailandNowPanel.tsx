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

type WpDraft = { image_url: string; title: string; alt_text: string; caption: string };

function bareDomain(u: string): string {
  try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; }
}

function wpDefaults(im: any, tier: 1 | 2, articleUrl: string): WpDraft {
  const src = tier === 1 ? bareDomain(articleUrl) : `${im.provider || "stock"}.com`;
  const nameFromUrl = (() => {
    try { return decodeURIComponent(new URL(im.url).pathname.split("/").pop() || "").replace(/\.[a-z0-9]+$/i, "").replace(/[-_]+/g, " ").trim(); }
    catch { return ""; }
  })();
  return {
    image_url: im.url,
    title: tier === 1 ? (nameFromUrl || im.alt || "article image") : `${(im.provider || "stock")} photo ${im.w}x${im.h}`,
    alt_text: tier === 1 ? (im.alt || "") : "",
    caption: `Source: ${src} / Website`,
  };
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
}
interface ArchiveReply {
  answer: string;
  sources: ArchiveSource[];
  mode: "direct" | "synthesized" | "degraded";
}
interface ArchiveMsg {
  q: string;
  reply: ArchiveReply;
}

// STORY SCOUT types
type ScoutResult = { title: string; url: string; snippet: string; date: string; lang: string; source: string };
type PitchReply = { pitch: { headline_en: string; headline_th: string; excerpt_en: string }; mode: string };

// FIRESIDE types
interface FiresideTopic {
  title: string;
  angle: string;
  ep_adjacent: string[];
  source_urls: string[];
  if_like_a_try_b: string;
  visual_style: string;
  why_fresh: string;
  revisit_candidate: boolean;
}

interface FiresideFix {
  anchor: string;
  note: string;
  severity: "must" | "should" | "nit";
}

interface FiresideNotes {
  overall?: string;
  strengths?: string[];
  fixes?: FiresideFix[];
  structure_notes?: string;
  voice_notes?: string;
  coverage_check?: string;
}

interface FiresideSourceReport {
  topics: FiresideTopic[];
  mode: "notebook" | "web-fallback";
  notebook_id?: string;
}

interface FiresideEditNotesResp {
  notes: FiresideNotes;
  mode: "direct" | "degraded";
  error?: string;
}

// SEO HEALTH report (THAILAND NOW → SEO tab → HEALTH)
interface HealthSource { from: string; from_id?: number; from_title?: string }
interface HealthSuggestion { link: string; title: string; id?: number }
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
    ? <a className="seo-icon seo-icon--edit" href={u} target="_blank" rel="noreferrer" title="Edit in WordPress editor">✎</a>
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
  return (
    <>
      <div className="flex items-center gap-2 mb-2">
        <span className="label">HEALTH</span>
        <span className="mono" style={{ color: "var(--color-muted)" }}>
          link/image/orphan report &amp; 1-click fixes
        </span>
      </div>
      <HealthSubTab />
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

/** Optimistic local trim: drop fixed (post_id,target) rows from the cached report
 *  so counts shrink the instant a fix applies — no re-scan needed to see progress.
 *  Only trims fixes the server confirmed (removed = matches>0); no-ops leave the row. */
function trimFixed(
  r: HealthReport,
  fixes: { post_id: number; kind: "link" | "image"; target: string; removed: boolean }[],
): HealthReport {
  const links = new Map<number, Set<string>>();   // postId -> link targets removed
  const imgs = new Map<number, Set<string>>();    // postId -> image targets removed
  const extUrls = new Set<string>();              // external urls touched (key the ext record)
  for (const f of fixes) {
    if (!f.removed) continue;
    const bucket = f.kind === "image" ? imgs : links;
    if (!bucket.has(f.post_id)) bucket.set(f.post_id, new Set());
    bucket.get(f.post_id)!.add(f.target);
    if (f.kind === "link") extUrls.add(f.target);
  }
  const has = (m: Map<number, Set<string>>, pid?: number, t = "") =>
    !!pid && !!m.get(pid)?.has(t);

  const broken_internal_links = r.broken_internal_links.filter(
    (b) => !has(links, b.from_id, b.href || b.to));
  const broken_internal_images = r.broken_internal_images.filter(
    (b) => !has(imgs, b.from_id, b.src));

  // external links aggregate by url with a from[] source list: drop the trimmed
  // source post, and drop the whole record once its last known source is gone.
  let broken_external_links = r.broken_external_links;
  if (extUrls.size) {
    const next: typeof broken_external_links = [];
    for (const b of broken_external_links) {
      if (!extUrls.has(b.url)) { next.push(b); continue; }
      const from = (b.from || []).filter((f) => !has(links, f.from_id, b.url));
      if (from.length) next.push({ ...b, from });
    }
    broken_external_links = next;
  }
  return { ...r, broken_internal_links, broken_internal_images, broken_external_links };
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

interface ActivePreview {
  key: string;
  postId: number;
  kind: "link" | "image";
  target: string;
  loading: boolean;
  data?: { matches: number; before: string; after: string };
  error?: string;
  applied?: boolean;
}

interface SeoFixItem {
  post_id: number;
  kind: "link" | "image";
  target: string;
}

/** Orphan un-orphan flow (inbound direction): ANALYZE a suggested host → anchor
 *  candidates from the orphan's title → pick one → preview/confirm insert. */
interface AnchorData {
  hostId: number;
  orphanLink: string;
  loading: boolean;
  error?: string;
  candidates: { phrase: string; count: number; snippet: string }[];
}

interface InsertPreview {
  key: string;
  hostId: number;
  phrase: string;
  href: string;
  loading: boolean;
  data?: { matches: number; before: string; after: string };
  error?: string;
  applied?: boolean;
}

function PreviewBlock({
  activePreview,
  confirmLabel = "CONFIRM REMOVE",
  appliedLabel = "✓ Removed from WP content!",
  onApply,
  onClose,
}: {
  activePreview: ActivePreview;
  confirmLabel?: string;
  appliedLabel?: string;
  onApply: () => void;
  onClose: () => void;
}) {
  return (
    <div className="mt-1.5 p-2 border border-edge bg-shade flex flex-col gap-1.5 rounded">
      <div className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
        FIX PREVIEW — Post #{activePreview.postId}
      </div>
      {activePreview.loading && <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>Loading preview...</div>}
      {activePreview.error && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{activePreview.error}</div>}
      {activePreview.applied && (
        <div className="mono text-xs font-bold" style={{ color: "var(--color-go)" }}>
          {appliedLabel} Re-scan to update report.
        </div>
      )}
      {!activePreview.loading && !activePreview.applied && activePreview.data && (
        <>
          <div className="mono text-xs">Matches found in raw HTML: {activePreview.data.matches}</div>
          {activePreview.data.matches === 0 ? (
            <div className="mono text-xs" style={{ color: "var(--color-hazard)" }}>
              0 matches found in content.raw (may already be removed).
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              <div className="mono text-xs text-muted">BEFORE:</div>
              <pre
                className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap"
                style={{ background: "rgba(255,0,0,0.1)", border: "1px solid var(--color-critical)" }}
              >
                {activePreview.data.before}
              </pre>
              <div className="mono text-xs text-muted">AFTER:</div>
              <pre
                className="mono text-xs p-1.5 overflow-x-auto whitespace-pre-wrap"
                style={{ background: "rgba(0,255,0,0.1)", border: "1px solid var(--color-go)" }}
              >
                {activePreview.data.after}
              </pre>
            </div>
          )}
          <div className="flex items-center gap-2 mt-1">
            <button
              className="btn btn--crit"
              disabled={activePreview.loading || activePreview.data.matches === 0}
              onClick={onApply}
            >
              {confirmLabel}
            </button>
            <button className="btn" onClick={onClose}>
              CANCEL
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function BulkConfirm({
  title,
  description,
  getItems,
  bulkProgress,
  setBulkProgress,
  onClose,
  onApplied,
}: {
  title: string;
  description: string;
  getItems: () => SeoFixItem[];
  bulkProgress: string | null;
  setBulkProgress: (s: string | null) => void;
  onClose: () => void;
  onApplied: (fixes: { post_id: number; kind: "link" | "image"; target: string; removed: boolean }[]) => void;
}) {
  return (
    <div className="p-2 border border-critical bg-shade flex flex-col gap-2 my-1">
      <div className="mono text-xs font-bold" style={{ color: "var(--color-critical)" }}>
        {title}
      </div>
      <div className="mono text-xs text-muted">{description}</div>
      {bulkProgress && <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>{bulkProgress}</div>}
      <div className="flex gap-2">
        <button
          className="btn btn--crit"
          disabled={!!bulkProgress}
          onClick={async () => {
            setBulkProgress("Applying bulk removal...");
            const items = getItems();
            const res = await post<{
              successful: number; total: number; removed: number; noop: number; failed: number;
              results: { ok: boolean; matches: number; post_id: number }[];
            }>("/api/thailandnow/seo/apply-fix-bulk", { items });
            if (res.ok && res.data) {
              const d = res.data;
              onApplied(items.map((it, i) => ({
                post_id: it.post_id, kind: it.kind, target: it.target,
                removed: (d.results[i]?.matches ?? 0) > 0,
              })));
              const parts = [`Removed ${d.removed}/${d.total}`];
              if (d.noop) parts.push(`${d.noop} already gone`);
              if (d.failed) parts.push(`${d.failed} failed`);
              setBulkProgress(parts.join(" · ") + ".");
            } else {
              setBulkProgress(`Error: ${res.error || "failed"}`);
            }
          }}
        >
          CONFIRM BULK REMOVE
        </button>
        <button className="btn" onClick={onClose}>
          CANCEL
        </button>
      </div>
    </div>
  );
}

function HealthSubTab() {
  const { data: jobsData, refetch: refetchJobs } = usePolling<{ jobs: TnJob[] }>("/api/thailandnow/jobs", 2000);
  const [report, setReport] = usePersistentState<HealthReport | null>("tn.seo.health.report", null);
  const [err, setErr] = useState<string | null>(null);
  
  const jobs = jobsData?.jobs ?? [];
  const scanJob = jobs.find((j) => j.kind === "seo-health") ?? null;
  const scanning = !!scanJob && (scanJob.status === "queued" || scanJob.status === "running");

  // Slice 2: Preview / Fix / Dismiss / Bulk states
  const [activePreview, setActivePreview] = useState<ActivePreview | null>(null);

  const [dismissed, setDismissed] = usePersistentState<string[]>("tn.seo.dismissed", []);
  const [showDismissed, setShowDismissed] = useState(false);
  const [bulkConfirmSection, setBulkConfirmSection] = useState<string | null>(null);
  const [bulkProgress, setBulkProgress] = useState<string | null>(null);

  // Orphan un-orphan flow (inbound): expand a row → ANALYZE a host → pick an
  // anchor phrase → preview insert → CONFIRM INSERT (link TO the orphan).
  const [expandedOrphan, setExpandedOrphan] = useState<string | null>(null);
  const [anchorData, setAnchorData] = useState<AnchorData | null>(null);
  const [insertPreview, setInsertPreview] = useState<InsertPreview | null>(null);

  const trimFixedFromReport = useCallback(
    (fixes: { post_id: number; kind: "link" | "image"; target: string; removed: boolean }[]) =>
      setReport((prev) => (prev ? trimFixed(prev, fixes) : prev)),
    [setReport],
  );

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
      if ((res.data?.matches ?? 0) > 0) {
        setReport((prev) => prev ? trimFixed(prev, [{ post_id: postId, kind, target, removed: true }]) : prev);
      }
    } else {
      setActivePreview((prev) => prev ? { ...prev, loading: false, error: res.error || "Failed to apply fix" } : null);
    }
  };

  const handleAnalyze = async (hostId: number, orphanTitle: string, orphanLink: string) => {
    setInsertPreview(null);
    setAnchorData({ hostId, orphanLink, loading: true, candidates: [] });
    const res = await post<{ candidates: { phrase: string; count: number; snippet: string }[] }>(
      "/api/thailandnow/seo/analyze-anchors",
      { host_id: hostId, orphan_title: orphanTitle, orphan_link: orphanLink },
    );
    if (res.ok && res.data) {
      setAnchorData({ hostId, orphanLink, loading: false, candidates: res.data.candidates });
    } else {
      setAnchorData({ hostId, orphanLink, loading: false, error: res.error || "Failed to analyze", candidates: [] });
    }
  };

  const handlePreviewInsert = async (key: string, hostId: number, phrase: string, href: string) => {
    setInsertPreview({ key, hostId, phrase, href, loading: true });
    const res = await post<{ matches: number; before: string; after: string }>(
      "/api/thailandnow/seo/preview-insert",
      { host_id: hostId, phrase, href },
    );
    if (res.ok && res.data) {
      setInsertPreview({ key, hostId, phrase, href, loading: false, data: res.data });
    } else {
      setInsertPreview({ key, hostId, phrase, href, loading: false, error: res.error || "Failed to load preview" });
    }
  };

  const handleApplyInsert = async () => {
    if (!insertPreview || !insertPreview.data) return;
    const { hostId, phrase, href } = insertPreview;
    const orphanLink = anchorData?.orphanLink;
    setInsertPreview((prev) => prev ? { ...prev, loading: true } : null);
    const res = await post<{ ok: boolean; matches: number }>("/api/thailandnow/seo/apply-insert", {
      host_id: hostId, phrase, href,
    });
    if (res.ok) {
      if ((res.data?.matches ?? 0) > 0) {
        setReport((prev) => prev && orphanLink
          ? { ...prev, orphans: prev.orphans.filter((o) => o.link !== orphanLink) }
          : prev);
        setAnchorData(null);
        setInsertPreview(null);
        setExpandedOrphan(null);
      } else {
        setInsertPreview((prev) => prev ? {
          ...prev, loading: false,
          error: "no valid spot found — nothing inserted (content may have changed since preview)",
        } : null);
      }
    } else {
      setInsertPreview((prev) => prev ? { ...prev, loading: false, error: res.error || "Failed to apply insert" } : null);
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
            hint="zero inbound internal links — the SEO priority. ✎ opens the editor to un-orphan by hand; click an article to analyze where to embed an inbound link.">
            {r.orphans.map((o) => {
              const expanded = expandedOrphan === o.link;
              return (
                <div key={o.link} className="text-sm mt-1 flex flex-col gap-0.5">
                  <div>
                    <a href={o.link} target="_blank" rel="noreferrer" style={{ color: "var(--color-phosphor)" }}
                      onClick={(e) => { e.preventDefault(); setExpandedOrphan(expanded ? null : o.link); }}>
                      {expanded ? "▾ " : "▸ "}{o.title || o.link}
                    </a>
                    <a href={o.link} target="_blank" rel="noreferrer" title="open article"
                      className="mono text-xs ml-1" style={{ color: "var(--color-muted)" }}>↗</a>
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
                  {expanded && o.suggested.map((s) => {
                    const analyzing = !!anchorData && !!s.id && anchorData.hostId === s.id && anchorData.orphanLink === o.link;
                    return (
                      <div key={s.link} className="flex flex-col gap-0.5 pl-3">
                        <div className="mono text-xs flex items-center gap-1" style={{ color: "var(--color-muted)" }}>
                          <span>▸ {s.title}</span>
                          {s.id && (
                            <button className="btn btn--compact" onClick={() => handleAnalyze(s.id!, o.title, o.link)}>
                              ANALYZE
                            </button>
                          )}
                        </div>
                        {analyzing && anchorData!.loading && (
                          <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>Analyzing…</div>
                        )}
                        {analyzing && anchorData!.error && (
                          <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{anchorData!.error}</div>
                        )}
                        {analyzing && !anchorData!.loading && !anchorData!.error && anchorData!.candidates.length === 0 && (
                          <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>no anchor candidates found</div>
                        )}
                        {analyzing && anchorData!.candidates.map((c) => {
                          const ipKey = `${o.link}|${s.link}|${c.phrase}`;
                          return (
                            <div key={c.phrase} className="flex flex-col gap-0.5 pl-3">
                              <button className="btn btn--compact mono text-xs"
                                onClick={() => handlePreviewInsert(ipKey, s.id!, c.phrase, o.link)}>
                                {c.phrase} ×{c.count}
                              </button>
                              <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>{c.snippet}</div>
                              {insertPreview && insertPreview.key === ipKey && (
                                <PreviewBlock
                                  activePreview={{
                                    key: insertPreview.key,
                                    postId: insertPreview.hostId,
                                    kind: "link",
                                    target: insertPreview.phrase,
                                    loading: insertPreview.loading,
                                    data: insertPreview.data,
                                    error: insertPreview.error,
                                    applied: insertPreview.applied,
                                  }}
                                  confirmLabel="CONFIRM INSERT"
                                  appliedLabel="✓ Link inserted in WP content!"
                                  onApply={handleApplyInsert}
                                  onClose={() => setInsertPreview(null)}
                                />
                              )}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })}
                </div>
              );
            })}
          </HealthList>

          <HealthList title="BROKEN INTERNAL LINKS" count={r.broken_internal_links.length} accent="var(--color-hazard)"
            hint="target slug ∉ published set. ✎ opens the source post. Remove strips tag and keeps inner text."
            action={
              r.broken_internal_links.length > 0 && (
                <button
                  className="btn btn--compact btn--crit"
                  onClick={() => setBulkConfirmSection("internal")}
                >
                  REMOVE ALL ({r.broken_internal_links.length})
                </button>
              )
            }
          >
            {bulkConfirmSection === "internal" && (
              <BulkConfirm
                title={`CONFIRM BULK REMOVE (${r.broken_internal_links.length} Broken Internal Links)`}
                onApplied={trimFixedFromReport}
                description="Strips all matching broken internal links across source posts, preserving inner text."
                getItems={() =>
                  r.broken_internal_links
                    .filter((b) => b.from_id)
                    .map((b) => ({ post_id: b.from_id!, kind: "link" as const, target: b.href || b.to }))
                }
                bulkProgress={bulkProgress}
                setBulkProgress={setBulkProgress}
                onClose={() => { setBulkConfirmSection(null); setBulkProgress(null); }}
              />
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
                        className="seo-icon seo-icon--remove"
                        title="Remove this broken link"
                        onClick={() => handleStartPreview(rowKey, b.from_id!, "link", b.href || b.to)}
                      >
                        ✗
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
                    <PreviewBlock
                      activePreview={activePreview}
                      onApply={handleApplyFix}
                      onClose={() => setActivePreview(null)}
                    />
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
                  <button className="btn btn--compact" onClick={() => setShowDismissed((v) => !v)}>
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
                        className="seo-icon seo-icon--dismiss"
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
                  className="btn btn--compact btn--crit"
                  onClick={() => setBulkConfirmSection("external")}
                >
                  REMOVE ALL ({r.broken_external_links.length})
                </button>
              )
            }
          >
            {bulkConfirmSection === "external" && (
              <BulkConfirm
                title={`CONFIRM BULK REMOVE (${r.broken_external_links.length} Broken External Links)`}
                onApplied={trimFixedFromReport}
                description="Strips all matching broken external links across source posts, preserving inner text."
                getItems={() => {
                  const items: SeoFixItem[] = [];
                  for (const b of r.broken_external_links) {
                    if (b.from && b.from.length > 0) {
                      for (const f of b.from) {
                        if (f.from_id) items.push({ post_id: f.from_id, kind: "link", target: b.url });
                      }
                    }
                  }
                  return items;
                }}
                bulkProgress={bulkProgress}
                setBulkProgress={setBulkProgress}
                onClose={() => { setBulkConfirmSection(null); setBulkProgress(null); }}
              />
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
                        className="seo-icon seo-icon--remove"
                        title="Remove this broken link"
                        onClick={() => handleStartPreview(rowKey, src0.from_id!, "link", b.url)}
                      >
                        ✗
                      </button>
                    )}
                    {b.from && b.from.length > 1 && (
                      <span className="mono text-xs ml-2" style={{ color: "var(--color-muted)" }}>({b.from.length} posts)</span>
                    )}
                  </div>

                  {activePreview && activePreview.key === rowKey && (
                    <PreviewBlock
                      activePreview={activePreview}
                      onApply={handleApplyFix}
                      onClose={() => setActivePreview(null)}
                    />
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
                <button className="btn btn--compact" onClick={() => setShowDismissed((v) => !v)}>
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
                  className="btn btn--compact btn--crit"
                  onClick={() => setBulkConfirmSection("images")}
                >
                  REMOVE ALL ({r.broken_internal_images.length})
                </button>
              )
            }
          >
            {bulkConfirmSection === "images" && (
              <BulkConfirm
                title={`CONFIRM BULK REMOVE (${r.broken_internal_images.length} Broken Images)`}
                onApplied={trimFixedFromReport}
                description="Drops all matching broken img tags across source posts."
                getItems={() =>
                  r.broken_internal_images
                    .filter((b) => b.from_id)
                    .map((b) => ({ post_id: b.from_id!, kind: "image" as const, target: b.src }))
                }
                bulkProgress={bulkProgress}
                setBulkProgress={setBulkProgress}
                onClose={() => { setBulkConfirmSection(null); setBulkProgress(null); }}
              />
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
                        className="seo-icon seo-icon--remove"
                        title="Remove this broken image"
                        onClick={() => handleStartPreview(rowKey, b.from_id!, "image", b.src)}
                      >
                        ✗
                      </button>
                    )}
                  </div>

                  {activePreview && activePreview.key === rowKey && (
                    <PreviewBlock
                      activePreview={activePreview}
                      onApply={handleApplyFix}
                      onClose={() => setActivePreview(null)}
                    />
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
                <button className="btn btn--compact" onClick={() => setShowDismissed((v) => !v)}>
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

// --- TRAFFIC sub-module (daily GA4 cumulative totals → Analytics & Boosting sheet)
interface TrafficWrite {
  row: number;
  date: string;
  day: number;
  target_old: number | string | null; // echoed verbatim (formula or number)
  actual_old: number | null;
  actual_new: number;
  daily_old: number | string | null; // echoed verbatim (formula or number)
  daily_new: number | null;
  daily_is_formula: boolean;
}
interface TrafficAppend {
  date: string;
  day: number;
  target: number | string | null; // shifted formula, plain number, or null
  actual_new: number;
  daily_new: number | string | null; // shifted formula or computed diff
}
interface TrafficAnalyze {
  rows: TrafficWrite[];
  appends: TrafficAppend[];
  warnings: string[];
  text: string;
  generated_at: string;
  from: string;
  to: string;
}
interface TrafficApplyResp {
  ok: boolean;
  written: number;
  appended: number;
  failed: { row: number; error: string }[];
}

/** "Aug 21" label from an ISO date (matches the sheet's A column format). */
function trafficLabel(date: string): string {
  const d = new Date(date + "T00:00:00");
  return `${d.toLocaleString("en-US", { month: "short" })} ${d.getDate()}`;
}
function trafficNum(n: number | string | null | undefined): string {
  if (n === null || n === undefined || n === "") return "—";
  return typeof n === "number" ? n.toLocaleString("en-US") : n;
}
function trafficSigned(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${n >= 0 ? "+" : ""}${n.toLocaleString("en-US")}`;
}
/** Today as YYYY-MM-DD in the browser's local tz. */
function localToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function daysBetween(from: string, to: string): number {
  return Math.round((new Date(to + "T00:00:00").getTime() - new Date(from + "T00:00:00").getTime()) / 86400000);
}

function TrafficTab() {
  return (
    <>
      <div className="flex items-center gap-2 mb-2">
        <span className="label">TRAFFIC</span>
        <span className="mono" style={{ color: "var(--color-muted)" }}>
          daily GA4 cumulative totals → sheet diff
        </span>
      </div>
      <TrafficSubTab />
    </>
  );
}

function TrafficSubTab() {
  const [from, setFrom] = useState(localToday);
  const [to, setTo] = useState(localToday);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [data, setData] = useState<TrafficAnalyze | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [applyRes, setApplyRes] = useState<TrafficApplyResp | null>(null);
  const [applyErr, setApplyErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    setErr(null);
    setApplyRes(null);
    setConfirming(false);
    if (daysBetween(from, to) > 91) {
      setErr("range too wide — pick at most 92 dates");
      return;
    }
    setBusy(true);
    const r = await post<TrafficAnalyze>("/api/thailandnow/traffic/analyze", { from, to });
    setBusy(false);
    if (r.ok && r.data) setData(r.data);
    else setErr(r.error ?? "analyze failed");
  }, [from, to]);

  const apply = useCallback(async () => {
    if (!data) return;
    setApplyErr(null);
    const r = await post<TrafficApplyResp>("/api/thailandnow/traffic/apply", {
      sheet_writes: data.rows,
      appends: data.appends,
    });
    if (r.ok && r.data) {
      setApplyRes(r.data);
      setConfirming(false);
    } else {
      setApplyErr(r.error ?? "apply failed");
    }
  }, [data]);

  // 503 config-missing hint explains ga.json — render verbatim in muted, not as an error
  const configHint = err !== null && err.includes("ga.json");

  return (
    <section className="hud hud--bracket reveal reveal-1 p-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <input type="date" value={from} max={to} onChange={(e) => setFrom(e.target.value)} />
        <span className="mono">→</span>
        <input type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} />
        <button className="btn btn--signal" disabled={busy} onClick={run}>
          {busy ? "RUNNING…" : "RUN"}
        </button>
        {data && (
          <button className="btn" onClick={() => navigator.clipboard.writeText(data.text).catch(() => {})}>
            COPY TEXT
          </button>
        )}
        {data && (data.rows.length > 0 || data.appends.length > 0) && (
          <button className="btn btn--signal" onClick={() => setConfirming(true)}>
            WRITE SHEET
          </button>
        )}
      </div>

      {err && (
        <div className="mono" style={{ color: configHint ? "var(--color-muted)" : "var(--color-critical)" }}>
          {err}
        </div>
      )}

      {data && (
        <div className="flex flex-col gap-1">
          <div className="mono" style={{ color: "var(--color-muted)" }}>
            {data.rows.length + data.appends.length} days · generated {data.generated_at}
          </div>

          {confirming && (
            <div className="p-2 border border-critical bg-shade flex flex-col gap-2 my-1">
              <div className="mono text-xs font-bold" style={{ color: "var(--color-critical)" }}>
                CONFIRM SHEET WRITE ({data.rows.length} rows + {data.appends.length} appends)
              </div>
              <div className="mono text-xs text-muted">
                Writes column D (Actual) always and E (Daily) only where the cell has no
                formula; appends new rows past the sheet's last row.
              </div>
              <div className="flex gap-2">
                <button className="btn btn--crit" onClick={apply}>CONFIRM WRITE</button>
                <button className="btn" onClick={() => setConfirming(false)}>CANCEL</button>
              </div>
            </div>
          )}

          <div className="scroll-y flex flex-col gap-0.5">
            {data.rows.map((w) => (
              <div key={`${w.date}-${w.row}`} className="mono text-xs">
                {trafficLabel(w.date)} Day {w.day} — Actual: {trafficNum(w.actual_old)} →{" "}
                {w.actual_new.toLocaleString("en-US")}
                {" · Daily: "}{trafficNum(w.daily_old)}
                {w.daily_is_formula ? (
                  <span className="ml-1" style={{ color: "var(--color-muted)" }}>(formula, untouched)</span>
                ) : (
                  <> → {trafficSigned(w.daily_new)}</>
                )}
              </div>
            ))}
            {data.appends.map((a) => (
              <div key={a.date} className="mono text-xs">
                <span style={{ color: "var(--color-signal)" }}>NEW ROW</span>{" "}
                {trafficLabel(a.date)} Day {a.day} — Actual: → {a.actual_new.toLocaleString("en-US")}
                {" · Daily: "}{typeof a.daily_new === "number" ? trafficSigned(a.daily_new) : trafficNum(a.daily_new)}
              </div>
            ))}
          </div>

          {data.warnings.map((wn, i) => (
            <div key={i} className="mono text-xs" style={{ color: "var(--color-hazard)" }}>{wn}</div>
          ))}

          {applyRes && (
            <div className="mono text-xs font-bold" style={{ color: applyRes.ok ? "var(--color-go)" : "var(--color-critical)" }}>
              {applyRes.ok
                ? `✓ ${applyRes.written} written · ${applyRes.appended} appended`
                : `${applyRes.written} written · ${applyRes.appended} appended · ${applyRes.failed.length} failed`}
              {applyRes.failed.map((f, i) => (
                <div key={i} style={{ color: "var(--color-critical)", fontWeight: "normal" }}>
                  row {f.row}: {f.error}
                </div>
              ))}
            </div>
          )}
          {applyErr && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{applyErr}</div>}
        </div>
      )}
    </section>
  );
}

export default function ThailandNowPanel({ module: _module }: { module: ModuleConfig }) {
  const [tab, setTab] = useState<"writers" | "events" | "archive" | "seo" | "traffic" | "story-scout" | "wordpress">("writers");
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
          className={`btn btn--compact ${tab === "story-scout" ? "btn--signal" : ""}`}
          onClick={() => setTab("story-scout")}
        >
          STORY SCOUT
        </button>
        <button
          className={`btn btn--compact ${tab === "seo" ? "btn--signal" : ""}`}
          onClick={() => setTab("seo")}
        >
          SEO
        </button>
        <button // NEW: TRAFFIC button
          className={`btn btn--compact ${tab === "traffic" ? "btn--signal" : ""}`}
          onClick={() => setTab("traffic")}
        >
          TRAFFIC
        </button>
        <button
          className={`btn btn--compact ${tab === "wordpress" ? "btn--signal" : ""}`}
          onClick={() => setTab("wordpress")}
        >
          WORDPRESS OP
        </button>
      </div>

      {tab === "writers" && (
        <WritersTab desks={data?.desks ?? []} ready={data?.ready ?? false} loading={!data && !error} error={error} />
      )}
      {tab === "events" && <EventsTab />}
      {tab === "archive" && <ArchiveTab />} {/* NEW: ArchiveTab render */}
      {tab === "story-scout" && <StoryScoutTab />}
      {tab === "seo" && <SeoTab />}
      {tab === "traffic" && <TrafficTab />} {/* NEW: TrafficTab render */}
      {tab === "wordpress" && <WpOpTab />}
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

/** Unicode-aware slug matching backend `_covered_slug` (keeps Thai letters + digits). */
const slugCovered = (s: string) => (s || "").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");

function EventsTab() {
  // persisted: walk away, come back — results + the form that produced them survive
  const [mode, setMode] = usePersistentState<"scout" | "deep">("tn.mode", "scout");
  const [query, setQuery] = usePersistentState("tn.query", "");
  const [weeks, setWeeks] = usePersistentState("tn.weeks", 4);
  const [events, setEvents] = usePersistentState<TnEvent[]>("tn.events", []);
  const [hideCovered, setHideCovered] = usePersistentState<boolean>("tn.events.hideCovered", true);
  const [fetching, setFetching] = useState(false);
  const [busyLabel, setBusyLabel] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [pickedKey, setPickedKey] = useState<string | null>(null);
  const [selNb, setSelNb] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  const [manualUrl, setManualUrl] = useState("");
  const [manualTitle, setManualTitle] = useState("");
  const [manualErr, setManualErr] = useState<string | null>(null);
  const [harvesting, setHarvesting] = useState(false);
  // hub library (saved source pages) for quick re-scanning
  const [hubs, setHubs] = useState<{ url: string; title: string; mode: string; added?: string }[]>([]);
  const [hubsOpen, setHubsOpen] = useState(false);
  const [lastMode, setLastMode] = useState<"links" | "events">("links");
  const [scanning, setScanning] = useState(false);
  const [syncingRegistry, setSyncingRegistry] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const syncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    let alive = true;
    fetchJSON<{ hubs: { url: string; title: string; mode: string; added?: string }[] }>(
      "/api/thailandnow/events/hubs",
    )
      .then((r) => { if (alive) setHubs(r.hubs ?? []); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);
  const [notifyPerm, setNotifyPerm] = useState<NotificationPermission>(
    typeof Notification !== "undefined" ? Notification.permission : "denied",
  );
  // covered-events dedup set {slug: source} from OURS + COMPANY sheets — fetch on mount or registry sync
  const [covered, setCovered] = useState<Record<string, string>>({});
  const loadCovered = useCallback(async () => {
    try {
      const r = await fetchJSON<{ covered: Record<string, string>; errors: string[] }>(
        "/api/thailandnow/events/covered",
      );
      setCovered(r.covered ?? {});
    } catch {
      // non-fatal: no badge if the sheets are unreachable
    }
  }, []);
  useEffect(() => {
    loadCovered();
  }, [loadCovered]);

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

  // clear the 📋 IDE SCOUT and sync flash timers on unmount
  useEffect(() => () => {
    if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current);
    if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
  }, []);

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
  const sorted = useMemo(() => {
    const list = [...events].sort((a, b) => {
      const sa = a.start_date ?? "";
      const sb = b.start_date ?? "";
      return sa < sb ? -1 : sa > sb ? 1 : 0;
    });
    if (!hideCovered) return list;
    return list.filter((e) => {
      const src = covered[slugCovered(e.title)];
      return !(src === "ours" || src === "company");
    });
  }, [events, hideCovered, covered]);

  const hiddenCount = events.length - sorted.length;

  const handleSyncRegistry = useCallback(async () => {
    setSyncingRegistry(true);
    try {
      const r = await fetchJSON<{ published_synced: number; pipeline_flipped: number }>(
        "/api/thailandnow/events/registry/sync",
        { method: "POST" },
      );
      await loadCovered();
      const msg = `synced ${r.published_synced ?? 0} published (${r.pipeline_flipped ?? 0} flipped)`;
      setSyncMsg(msg);
      if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
      syncTimerRef.current = setTimeout(() => setSyncMsg(null), 4000);
    } catch (e: any) {
      setSyncMsg(`sync failed: ${e?.message || e}`);
      if (syncTimerRef.current) clearTimeout(syncTimerRef.current);
      syncTimerRef.current = setTimeout(() => setSyncMsg(null), 4000);
    } finally {
      setSyncingRegistry(false);
    }
  }, [loadCovered]);

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

  // 📋 IDE SCOUT — copy a paste-ready Antigravity prompt (reads the SHARED vault handoff
  // note, writes /tmp/thailand-now-events/latest.json). Brief "COPIED ✓" flash on success.
  const handleCopyAntigravityPrompt = useCallback(async () => {
    const q = query.trim();
    const promptText = `Read \`10-knowledge/thailandnow-events-antigravity-handoff.md\` in this vault. Scout UPCOMING Thailand events (start date within the next ${weeks} week(s), i.e. today through today+${weeks}w)${q ? ` focused on: ${q}` : ""}. Use your full web browsing/search — prefer TAT + reputable event listings + Thai-language sources, broadening to any reputable source. Extract each event into the exact JSON shape in that note, dedupe the same event across sources into one row (keep all URLs), drop anything with no start date or outside the window, and write the result to \`/tmp/thailand-now-events/latest.json\` in that exact shape. Do NOT create any Google Doc or Trello card — provisioning stays a human step in the panel.`;
    try {
      await navigator.clipboard.writeText(promptText);
      setCopied(true);
      if (copyTimeoutRef.current) clearTimeout(copyTimeoutRef.current);
      copyTimeoutRef.current = setTimeout(() => setCopied(false), 2000);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to copy prompt");
    }
  }, [query, weeks]);

  // CONVERT — POST the IDE handoff through the backend (window filter + multi-source dedup),
  // merge into events via the same keyOf map the free SCOUT uses.
  const runConvert = useCallback(async () => {
    setFetching(true);
    setErr(null);
    setBusyLabel("CONVERTING IDE handoff…");
    const r = await post<ScoutResp>("/api/thailandnow/events/convert", { query, weeks });
    setFetching(false);
    setBusyLabel(null);
    if (r.ok && r.data) {
      if (r.data.events.length === 0 && r.data.errors.length > 0) {
        setErr(r.data.errors[0]);
      } else {
        setEvents((prev) => {
          const map = new Map(prev.map((e) => [keyOf(e), e]));
          for (const e of r.data!.events) map.set(keyOf(e), e);
          return [...map.values()];
        });
      }
    } else {
      setErr(r.error ?? "CONVERT failed");
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

  // MANUAL ADD: a link (reuses the seed scrape) or a title-only entry → seed a TnEvent → open ThickBox
  const addManual = useCallback(async () => {
    const url = manualUrl.trim();
    const title = manualTitle.trim();
    if (!url && !title) {
      setManualErr("paste a link or a title");
      return;
    }
    setManualErr(null);
    setManualOpen(false);
    setManualUrl("");
    setManualTitle("");
    if (url) {
      await pickFromUrl(url); // scrapes the URL → opens ThickBox (manages seeding + pickedKey)
      return;
    }
    const ev: TnEvent = { title, url: "" };
    setEvents((prev) => {
      const map = new Map(prev.map((e) => [keyOf(e), e]));
      map.set(keyOf(ev), ev);
      return [...map.values()];
    });
    setPickedKey(keyOf(ev)); // opens ThickBox with empty dates — user fills START/END/SIGNUP
  }, [manualUrl, manualTitle, pickFromUrl]);

  const harvestPage = useCallback(async (mode: "links" | "events") => {
    const url = manualUrl.trim();
    if (!url) {
      setManualErr(mode === "events" ? "paste a listicle URL to harvest events" : "paste a listings URL to harvest");
      return;
    }
    setManualErr(null);
    setHarvesting(true);
    setLastMode(mode);
    try {
      const r = await post<{ events: TnEvent[]; count: number }>(
        "/api/thailandnow/deep/harvest", { url, mode },
      );
      const found = r.data?.events ?? [];
      if (!found.length) {
        setManualErr(mode === "events"
          ? "no inline events found — is this a listicle with event headings? (try HARVEST LINKS for an index page)"
          : "no event links found — try ADD & OPEN for a single event");
        return;
      }
      setEvents((prev) => {
        const map = new Map(prev.map((e) => [keyOf(e), e]));
        for (const e of found) map.set(keyOf(e), e); // dedupe by keyOf (url||title)
        return [...map.values()];
      });
      setManualOpen(false);
      setManualUrl("");
      setManualTitle("");
    } catch {
      setManualErr("harvest failed — retry, or paste the link directly with ADD & OPEN");
    } finally {
      setHarvesting(false);
    }
  }, [manualUrl]);

  const saveHub = useCallback(async () => {
    const url = manualUrl.trim();
    if (!url) { setManualErr("paste a URL first, then tap ★ HUB"); return; }
    setManualErr(null);
    try {
      const r = await post<{ hubs: { url: string; title: string; mode: string; added?: string }[] }>(
        "/api/thailandnow/events/hubs",
        { url, title: manualTitle.trim() || url, mode: lastMode },
      );
      setHubs(r.data?.hubs ?? hubs);
    } catch {
      setManualErr("could not save hub");
    }
  }, [manualUrl, manualTitle, lastMode, hubs]);

  const removeHub = useCallback(async (url: string) => {
    try {
      const r = await fetchJSON<{ hubs: { url: string; title: string; mode: string; added?: string }[] }>(
        `/api/thailandnow/events/hubs?url=${encodeURIComponent(url)}`,
        { method: "DELETE" },
      );
      setHubs(r.hubs ?? []);
    } catch { /* ignore */ }
  }, []);

  const scanHubs = useCallback(async () => {
    setScanning(true);
    setManualErr(null);
    try {
      const r = await post<{ events: TnEvent[]; count: number; hubs_scanned: number; errors: string[] }>(
        "/api/thailandnow/events/hubs/scan", {},
      );
      const found = r.data?.events ?? [];
      if (found.length) {
        setEvents((prev) => {
          const map = new Map(prev.map((e) => [keyOf(e), e]));
          for (const e of found) map.set(keyOf(e), e);
          return [...map.values()];
        });
      }
      setManualErr(found.length ? null : "no events from hubs");
      setHubsOpen(false);
    } catch {
      setManualErr("hub scan failed");
    } finally {
      setScanning(false);
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
            className={`btn btn--md ${mode === m ? "btn--signal" : ""}`}
            onClick={() => setMode(m)}
          >
            {m === "scout" ? "SCOUT" : "DEEP"}
          </button>
        ))}
        <button
          className={`btn btn--md ${manualOpen ? "btn--signal" : ""}`}
          onClick={() => setManualOpen((o) => !o)}
        >
          + ADD MANUAL
        </button>
        <button
          className={`btn btn--md ${hubsOpen ? "btn--signal" : ""}`}
          onClick={() => setHubsOpen((o) => !o)}
        >
          ★ HUBS{hubs.length ? ` (${hubs.length})` : ""}
        </button>
        <button
          className="btn btn--md"
          disabled={syncingRegistry}
          onClick={handleSyncRegistry}
        >
          {syncingRegistry ? "↻ SYNCING…" : "↻ SYNC REGISTRY"}
        </button>
        <span className="mono" style={{ color: "var(--color-muted)" }}>
          {syncMsg || (mode === "scout"
            ? "instant keyless search"
            : "NotebookLM research + on-demand extract")}
        </span>
      </div>

      {manualOpen && (
        <div className="hud p-3 mb-2 flex flex-col gap-2">
          <div className="label">▸ ADD EVENT MANUALLY — link or text</div>
          <input
            className="input"
            placeholder="paste an event link (auto-scrapes title + dates)…"
            value={manualUrl}
            onChange={(e) => setManualUrl(e.target.value)}
            disabled={seeding}
          />
          <input
            className="input"
            placeholder="…or type the event title (no link)"
            value={manualTitle}
            onChange={(e) => setManualTitle(e.target.value)}
            disabled={seeding}
          />
          {manualErr && (
            <div className="mono" style={{ color: "var(--color-critical)" }}>
              ⚠ {manualErr}
            </div>
          )}
          <div className="flex items-center gap-2 flex-wrap">
            <button
              className="btn btn--signal"
              disabled={seeding || harvesting}
              onClick={() => void addManual()}
            >
              {seeding ? "SEEDING…" : "▸ ADD & OPEN"}
            </button>
            <button
              className="btn"
              disabled={seeding || harvesting}
              onClick={() => void harvestPage("links")}
            >
              {harvesting ? "…" : "⇶ HARVEST LINKS"}
            </button>
            <button
              className="btn"
              disabled={seeding || harvesting}
              onClick={() => void harvestPage("events")}
              title="for a listicle whose events are headings/text, not separate links"
            >
              {harvesting ? "…" : "⇶ HARVEST EVENTS"}
            </button>
            <button
              className="btn btn--compact"
              disabled={seeding || harvesting || !manualUrl.trim()}
              onClick={() => void saveHub()}
              title="save this page as a hub to re-scan later (uses the last harvest mode)"
            >
              ★ HUB
            </button>
          </div>
        </div>
      )}

      {hubsOpen && (
        <div className="hud p-3 mb-2 flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="label">▸ SAVED HUBS — re-scan sources anytime</span>
            <button
              className="btn btn--compact btn--signal"
              disabled={scanning || !hubs.length}
              onClick={() => void scanHubs()}
            >
              {scanning ? "SCANNING…" : "⇶ SCAN ALL"}
            </button>
          </div>
          {hubs.length === 0 ? (
            <div className="mono" style={{ color: "var(--color-muted)" }}>
              no hubs yet — paste a page URL above and tap ★ HUB to save it (then SCAN ALL to pull fresh events from every saved source)
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {hubs.map((h) => (
                <div key={h.url} className="row-in flex items-center gap-2">
                  <span className="pip pip--signal" />
                  <span className="label">{h.mode === "events" ? "events" : "links"}</span>
                  <span
                    className="mono"
                    style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                  >
                    {h.title}
                  </span>
                  <button className="btn btn--compact btn--crit" onClick={() => void removeHub(h.url)}>
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

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
            <>
              <button className="btn btn--compact btn--signal" disabled={fetching} onClick={runScout}>
                {fetching && !busyLabel ? "SCANNING…" : "SCOUT"}
              </button>
              <button
                className="btn btn--compact"
                disabled={fetching}
                onClick={() => void handleCopyAntigravityPrompt()}
                title="Copy paste-ready prompt for Antigravity IDE scout"
              >
                {copied ? "COPIED ✓" : "📋 IDE SCOUT"}
              </button>
              <button
                className="btn btn--compact btn--signal"
                disabled={fetching}
                onClick={() => void runConvert()}
                title="Convert /tmp/thailand-now-events/latest.json handoff"
              >
                {fetching && busyLabel?.startsWith("CONVERT") ? "CONVERTING…" : "CONVERT"}
              </button>
            </>
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
            📋 IDE SCOUT copies a paste-ready prompt for Antigravity — paste it there, let it scout +
            write the handoff, then CONVERT to pull the deduped events back here. (SCOUT is still the
            instant keyless search; switch to DEEP for NotebookLM research.)
          </div>
        )}
      </section>

      <section className="hud hud--bracket reveal reveal-2 p-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="label">
            RESULTS{sorted.length ? ` · ${sorted.length}` : ""}
            {hiddenCount > 0 ? `, ${hiddenCount} covered hidden` : ""}
          </span>
          <div className="flex items-center gap-2">
            <button
              className={`btn btn--compact ${hideCovered ? "btn--signal" : ""}`}
              onClick={() => setHideCovered((v) => !v)}
            >
              {hideCovered ? "✕ HIDE COVERED" : "☐ HIDE COVERED"}
            </button>
            {events.length > 0 && (
              <button className="btn btn--compact btn--crit" onClick={() => setEvents([])}>
                CLEAR
              </button>
            )}
          </div>
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
                {(covered[slugCovered(e.title)] === "ours" || covered[slugCovered(e.title)] === "company") && (
                  <span
                    className="label"
                    style={{ color: "var(--color-signal)" }}
                    title={`already covered (${covered[slugCovered(e.title)]})`}
                  >
                    ✓ COVERED
                  </span>
                )}
                {covered[slugCovered(e.title)] === "pipeline" && (
                  <span
                    className="label"
                    style={{ color: "var(--color-go)" }}
                    title="in pipeline (see Pipeline tab)"
                  >
                    ⚙ PIPELINE
                  </span>
                )}
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


/* ------------------------------- FIRESIDE PANEL --------------------------- */

function FiresidePanel() {
  const [firesideSub, setFiresideSub] = usePersistentState<"fireside-source" | "fireside-edit">(
    "tn.scout.fireside.sub",
    "fireside-source"
  );

  // SOURCE TOPICS state
  const [seed, setSeed]               = usePersistentState<string>("tn.scout.fireside.seed", "");
  const [category, setCategory]       = usePersistentState<string>("tn.scout.fireside.category", "");
  const [topics, setTopics]           = usePersistentState<FiresideTopic[]>("tn.scout.fireside.topics", []);
  const [sourceMode, setSourceMode]   = usePersistentState<"notebook" | "web-fallback" | null>(
    "tn.scout.fireside.mode",
    null
  );
  const [sourceJobId, setSourceJobId] = usePersistentState<string | null>("tn.scout.fireside.job_id", null);
  const [searching, setSearching]     = useState(false);
  const [sourceErr, setSourceErr]     = useState<string | null>(null);
  const [copiedTopicIdx, setCopiedTopicIdx] = useState<number | null>(null);

  // EDIT NOTES state
  const [draft, setDraft]             = usePersistentState<string>("tn.scout.fireside.draft", "");
  const [url, setUrl]                 = usePersistentState<string>("tn.scout.fireside.url", "");
  const [checkCoverage, setCheckCoverage] = usePersistentState<boolean>("tn.scout.fireside.coverage", false);
  const [notesResp, setNotesResp]     = usePersistentState<FiresideEditNotesResp | null>(
    "tn.scout.fireside.notes_data",
    null
  );
  const [loadingNotes, setLoadingNotes] = useState(false);
  const [editErr, setEditErr]         = usePersistentState<string | null>("tn.scout.fireside.edit_err", null);
  const [notesCopied, setNotesCopied] = useState(false);

  // Polling for background jobs
  const { data: jobsData } = usePolling<{ jobs: TnJob[] }>("/api/thailandnow/jobs", 2000);

  // Track active job if one was already running
  useEffect(() => {
    if (sourceJobId && jobsData?.jobs) {
      const job = jobsData.jobs.find((j) => j.id === sourceJobId && j.kind === "fireside-source");
      if (job && (job.status === "running" || job.status === "queued")) {
        setSearching(true);
      }
    }
  }, [sourceJobId, jobsData]);

  // Handle completion / failure of fireside-source async job
  useEffect(() => {
    if (!sourceJobId || !jobsData?.jobs) return;
    const job = jobsData.jobs.find((j) => j.id === sourceJobId && j.kind === "fireside-source");
    if (!job) return;

    if (job.status === "done") {
      fetchJSON<FiresideSourceReport>(`/api/thailandnow/scout/fireside/source/report/${sourceJobId}`)
        .then((data) => {
          setTopics(data.topics || []);
          setSourceMode(data.mode || "notebook");
          setSearching(false);
          setSourceJobId(null);
        })
        .catch((e) => {
          setSourceErr(String(e));
          setSearching(false);
          setSourceJobId(null);
        });
    } else if (job.status === "error" || job.status === "cancelled") {
      setSourceErr(job.error || `job ${job.status}`);
      setSearching(false);
      setSourceJobId(null);
    }
  }, [sourceJobId, jobsData, setTopics, setSourceMode, setSourceJobId]);

  const sourceTopics = useCallback(async () => {
    setSourceErr(null);
    const r = await post<{ id: string }>("/api/thailandnow/scout/fireside/source", {
      seed: seed.trim() || undefined,
      category: category.trim() || undefined,
    });
    if (!r.ok) {
      setSourceErr(
        r.error?.includes("already running")
          ? "A FIRESIDE topic sourcing job is already running…"
          : r.error || "Failed to start topic sourcing"
      );
      return;
    }
    setTopics([]);
    setSourceMode(null);
    setSourceJobId(r.data!.id);
    setSearching(true);
  }, [seed, category, setTopics, setSourceMode, setSourceJobId]);

  const getEditNotes = useCallback(async () => {
    if (!draft.trim() && !url.trim()) {
      setEditErr("Please paste a draft script or enter a document URL.");
      return;
    }
    setEditErr(null);
    setLoadingNotes(true);
    const r = await post<FiresideEditNotesResp>("/api/thailandnow/scout/fireside/edit-notes", {
      draft: draft.trim() || undefined,
      url: url.trim() || undefined,
      check_coverage: checkCoverage,
    });
    setLoadingNotes(false);
    if (r.ok && r.data) {
      setNotesResp(r.data);
      if (r.data.error) {
        setEditErr(r.data.error);
      }
    } else {
      setEditErr(r.error || "Failed to generate editorial notes.");
    }
  }, [draft, url, checkCoverage, setNotesResp, setEditErr]);

  const copyTopic = useCallback((topic: FiresideTopic, idx: number) => {
    const parts = [
      `# ${topic.title}`,
      `**Angle:** ${topic.angle}`,
      topic.why_fresh ? `**Why Fresh:** ${topic.why_fresh}` : "",
      topic.if_like_a_try_b ? `**If You Liked A, Try B:** ${topic.if_like_a_try_b}` : "",
      topic.visual_style ? `**Visual Style:** ${topic.visual_style}` : "",
      topic.ep_adjacent?.length ? `**Adjacent Episodes:** ${topic.ep_adjacent.join(", ")}` : "",
      topic.source_urls?.length ? `**Sources:**\n${topic.source_urls.map((u) => `- ${u}`).join("\n")}` : "",
      topic.revisit_candidate ? `*[Revisit Candidate]*` : "",
    ].filter(Boolean);
    navigator.clipboard.writeText(parts.join("\n\n")).catch(() => {});
    setCopiedTopicIdx(idx);
    setTimeout(() => setCopiedTopicIdx(null), 2000);
  }, []);

  const copyNotes = useCallback(() => {
    if (!notesResp?.notes) return;
    const n = notesResp.notes;
    const parts: string[] = [];
    if (n.overall) parts.push(`## Overall Assessment\n${n.overall}`);
    if (n.strengths?.length) parts.push(`## Strengths\n${n.strengths.map((s) => `- ${s}`).join("\n")}`);
    if (n.fixes?.length) {
      parts.push(
        `## Line Fixes & Edits\n${n.fixes
          .map((f) => `### [${f.severity.toUpperCase()}] "${f.anchor}"\n${f.note}`)
          .join("\n\n")}`
      );
    }
    if (n.structure_notes) parts.push(`## Structure & Pacing\n${n.structure_notes}`);
    if (n.voice_notes) parts.push(`## Voice Notes (Ben Rujopakarn Tone)\n${n.voice_notes}`);
    if (n.coverage_check) parts.push(`## Past Episode Coverage Check\n${n.coverage_check}`);
    navigator.clipboard.writeText(parts.join("\n\n")).catch(() => {});
    setNotesCopied(true);
    setTimeout(() => setNotesCopied(false), 2000);
  }, [notesResp]);

  return (
    <div className="flex flex-col flex-grow">
      {/* Sub-mode toggle row */}
      <div className="flex items-center justify-between mb-3 shrink-0">
        <div className="flex items-center gap-2">
          <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
            {firesideSub === "fireside-source" ? "SOURCE TOPICS" : "EDIT NOTES"}
          </span>
          <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
            {firesideSub === "fireside-source"
              ? "— Ben-anchored Fireside show topic sourcing"
              : "— Ben Rujopakarn editorial notes & voice check"}
          </span>
        </div>
        <div className="flex items-center gap-1">
          <button
            className={`btn btn--compact ${firesideSub === "fireside-source" ? "btn--signal" : ""}`}
            onClick={() => setFiresideSub("fireside-source")}
          >
            SOURCE TOPICS
          </button>
          <button
            className={`btn btn--compact ${firesideSub === "fireside-edit" ? "btn--signal" : ""}`}
            onClick={() => setFiresideSub("fireside-edit")}
          >
            EDIT NOTES
          </button>
        </div>
      </div>

      {firesideSub === "fireside-source" ? (
        /* =================== SOURCE TOPICS SUB-VIEW =================== */
        <>
          {/* Input Controls */}
          <div className="flex flex-wrap items-center gap-2 shrink-0 mb-3">
            <input
              className="input"
              style={{ flexGrow: 1, minWidth: 220 }}
              placeholder="seed topic or keyword (optional, e.g. soft power, visa reforms)"
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey || !e.shiftKey)) {
                  e.preventDefault();
                  sourceTopics();
                }
              }}
            />
            <select
              className="input"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              <option value="">All Categories</option>
              <option value="expat-policy">Expat policy</option>
              <option value="business-investment">Business &amp; investment</option>
              <option value="lifestyle">Lifestyle</option>
              <option value="culture">Culture</option>
              <option value="infrastructure">Infrastructure</option>
            </select>
            <button
              className="btn btn--compact btn--signal"
              disabled={searching}
              onClick={sourceTopics}
            >
              {searching ? "SOURCING…" : "SOURCE TOPICS"}
            </button>
          </div>

          {/* Results Area */}
          <div className="scroll-y flex-grow flex flex-col gap-3">
            {sourceErr && (
              <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>
                {sourceErr}
              </div>
            )}
            {searching && (
              <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>
                Sourcing Fireside topics from NotebookLM corpus &amp; web references…
              </div>
            )}
            {!searching && topics.length === 0 && !sourceErr && (
              <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                Enter an optional seed topic or category and click SOURCE TOPICS.
              </div>
            )}

            {topics.length > 0 && (
              <div className="flex items-center justify-between shrink-0">
                <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                  TOPIC CANDIDATES ({topics.length})
                </span>
                {sourceMode && (
                  <span
                    className="mono text-xs px-1.5 py-0.5 rounded font-bold"
                    style={{
                      background:
                        sourceMode === "notebook"
                          ? "color-mix(in srgb, var(--color-go) 15%, var(--color-void))"
                          : "color-mix(in srgb, var(--color-hazard) 15%, var(--color-void))",
                      color: sourceMode === "notebook" ? "var(--color-go)" : "var(--color-hazard)",
                      border: `1px solid ${sourceMode === "notebook" ? "var(--color-go)" : "var(--color-hazard)"}`,
                    }}
                  >
                    {sourceMode === "notebook" ? "NOTEBOOK CORPUS" : "WEB FALLBACK"}
                  </span>
                )}
              </div>
            )}

            {topics.map((topic, idx) => (
              <div key={idx} className="border border-edge bg-void p-3 flex flex-col gap-2 rounded">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-bold text-sm" style={{ color: "var(--color-signal)" }}>
                      {topic.title}
                    </span>
                    {topic.revisit_candidate && (
                      <span
                        className="mono text-xs px-1.5 py-0.5 rounded font-bold"
                        style={{
                          background: "color-mix(in srgb, var(--color-hazard) 20%, var(--color-surface))",
                          color: "var(--color-hazard)",
                          border: "1px solid var(--color-hazard)",
                        }}
                      >
                        REVISIT CANDIDATE
                      </span>
                    )}
                  </div>
                  <button className="btn btn--compact" onClick={() => copyTopic(topic, idx)}>
                    {copiedTopicIdx === idx ? "✓ COPIED" : "COPY TOPIC"}
                  </button>
                </div>

                {topic.angle && (
                  <div className="text-xs" style={{ color: "var(--color-phosphor)" }}>
                    <span className="mono font-bold" style={{ color: "var(--color-signal)" }}>
                      ANGLE:{" "}
                    </span>
                    {topic.angle}
                  </div>
                )}

                {topic.why_fresh && (
                  <div className="text-xs" style={{ color: "var(--color-phosphor-dim)" }}>
                    <span className="mono font-bold" style={{ color: "var(--color-muted)" }}>
                      WHY FRESH:{" "}
                    </span>
                    {topic.why_fresh}
                  </div>
                )}

                {topic.if_like_a_try_b && (
                  <div className="text-xs" style={{ color: "var(--color-phosphor-dim)" }}>
                    <span className="mono font-bold" style={{ color: "var(--color-muted)" }}>
                      IF LIKE A, TRY B:{" "}
                    </span>
                    {topic.if_like_a_try_b}
                  </div>
                )}

                {topic.visual_style && (
                  <div className="text-xs" style={{ color: "var(--color-phosphor-dim)" }}>
                    <span className="mono font-bold" style={{ color: "var(--color-muted)" }}>
                      VISUAL STYLE:{" "}
                    </span>
                    {topic.visual_style}
                  </div>
                )}

                {topic.ep_adjacent && topic.ep_adjacent.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1 mt-1">
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                      ADJACENT:
                    </span>
                    {topic.ep_adjacent.map((ep, eIdx) => (
                      <span
                        key={eIdx}
                        className="mono text-xs px-1.5 py-0.5 rounded"
                        style={{
                          background: "var(--color-surface)",
                          color: "var(--color-phosphor-dim)",
                          border: "1px solid var(--color-edge)",
                        }}
                      >
                        {ep}
                      </span>
                    ))}
                  </div>
                )}

                {topic.source_urls && topic.source_urls.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2 mt-1">
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                      SOURCES:
                    </span>
                    {topic.source_urls.map((u, uIdx) => (
                      <a
                        key={uIdx}
                        href={u}
                        target="_blank"
                        rel="noreferrer"
                        className="mono text-xs hover:underline truncate max-w-xs"
                        style={{ color: "var(--color-signal)" }}
                      >
                        {bareDomain(u) || u}
                      </a>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      ) : (
        /* =================== EDIT NOTES SUB-VIEW =================== */
        <div className="flex flex-col flex-grow gap-3">
          {/* Inputs */}
          <div className="flex flex-col gap-2 shrink-0">
            <textarea
              className="input"
              rows={6}
              placeholder="Paste episode draft script here..."
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              style={{ width: "100%", resize: "vertical" }}
            />
            <div className="flex flex-wrap items-center gap-3">
              <input
                className="input"
                style={{ flexGrow: 1, minWidth: 240 }}
                placeholder="Or document / draft URL (e.g. Google Docs link or article URL)"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
              <label
                className="mono text-xs flex items-center gap-1 cursor-pointer"
                style={{ color: "var(--color-muted)" }}
              >
                <input
                  type="checkbox"
                  checked={checkCoverage}
                  onChange={(e) => setCheckCoverage(e.target.checked)}
                />
                Check past episode coverage
              </label>
              <button
                className="btn btn--signal"
                disabled={loadingNotes || (!draft.trim() && !url.trim())}
                onClick={getEditNotes}
              >
                {loadingNotes ? "ANALYZING DRAFT…" : "GET EDIT NOTES"}
              </button>
            </div>
          </div>

          {/* Error / Status */}
          {editErr && (
            <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>
              {editErr}
            </div>
          )}
          {loadingNotes && (
            <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>
              Analyzing draft script in Ben Rujopakarn's editorial voice…
            </div>
          )}

          {/* Notes Content */}
          <div className="scroll-y flex-grow flex flex-col gap-3">
            {!loadingNotes && !notesResp && !editErr && (
              <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                Paste a script draft above or provide a URL to get Ben-anchored editorial notes, line fixes, and pacing feedback.
              </div>
            )}

            {notesResp && (
              <div className="border border-edge bg-void p-3 flex flex-col gap-3 rounded">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                      EDITORIAL NOTES
                    </span>
                    <span
                      className="mono text-xs px-1.5 py-0.5 rounded font-bold"
                      style={{
                        background:
                          notesResp.mode === "direct"
                            ? "color-mix(in srgb, var(--color-go) 15%, var(--color-void))"
                            : "color-mix(in srgb, var(--color-hazard) 15%, var(--color-void))",
                        color: notesResp.mode === "direct" ? "var(--color-go)" : "var(--color-hazard)",
                        border: `1px solid ${
                          notesResp.mode === "direct" ? "var(--color-go)" : "var(--color-hazard)"
                        }`,
                      }}
                    >
                      {notesResp.mode === "direct" ? "DIRECT (BEN VOICE)" : "DEGRADED MODE"}
                    </span>
                  </div>
                  <button className="btn btn--compact" onClick={copyNotes}>
                    {notesCopied ? "✓ COPIED" : "COPY NOTES"}
                  </button>
                </div>

                {notesResp.error && (
                  <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>
                    {notesResp.error}
                  </div>
                )}

                {notesResp.notes?.overall && (
                  <div className="p-2 border border-edge bg-surface rounded flex flex-col gap-1">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-signal)" }}>
                      OVERALL ASSESSMENT
                    </span>
                    <div className="text-xs" style={{ color: "var(--color-phosphor)", whiteSpace: "pre-wrap" }}>
                      {notesResp.notes.overall}
                    </div>
                  </div>
                )}

                {notesResp.notes?.strengths && notesResp.notes.strengths.length > 0 && (
                  <div className="p-2 border border-edge bg-surface rounded flex flex-col gap-1">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-go)" }}>
                      STRENGTHS
                    </span>
                    <ul className="list-disc list-inside text-xs flex flex-col gap-1" style={{ color: "var(--color-phosphor)" }}>
                      {notesResp.notes.strengths.map((str, sIdx) => (
                        <li key={sIdx}>{str}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {notesResp.notes?.fixes && notesResp.notes.fixes.length > 0 && (
                  <div className="p-2 border border-edge bg-surface rounded flex flex-col gap-2">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-hazard)" }}>
                      LINE FIXES &amp; EDITS ({notesResp.notes.fixes.length})
                    </span>
                    <div className="flex flex-col gap-2">
                      {notesResp.notes.fixes.map((fix, fIdx) => {
                        const sevColor =
                          fix.severity === "must"
                            ? "var(--color-critical)"
                            : fix.severity === "should"
                            ? "var(--color-hazard)"
                            : "var(--color-muted)";
                        return (
                          <div
                            key={fIdx}
                            className="p-2 border border-edge bg-void rounded flex flex-col gap-1 text-xs"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span
                                className="mono text-xs uppercase px-1 py-0.5 rounded font-bold"
                                style={{ border: `1px solid ${sevColor}`, color: sevColor }}
                              >
                                {fix.severity}
                              </span>
                              {fix.anchor && (
                                <span
                                  className="mono text-xs italic truncate flex-grow"
                                  style={{ color: "var(--color-muted)" }}
                                >
                                  "{fix.anchor}"
                                </span>
                              )}
                            </div>
                            <div style={{ color: "var(--color-phosphor)" }}>{fix.note}</div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {notesResp.notes?.structure_notes && (
                  <div className="p-2 border border-edge bg-surface rounded flex flex-col gap-1">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-signal)" }}>
                      STRUCTURE &amp; PACING
                    </span>
                    <div className="text-xs" style={{ color: "var(--color-phosphor)", whiteSpace: "pre-wrap" }}>
                      {notesResp.notes.structure_notes}
                    </div>
                  </div>
                )}

                {notesResp.notes?.voice_notes && (
                  <div className="p-2 border border-edge bg-surface rounded flex flex-col gap-1">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor-dim)" }}>
                      VOICE &amp; BEN RUJOPAKARN TONE
                    </span>
                    <div className="text-xs" style={{ color: "var(--color-phosphor)", whiteSpace: "pre-wrap" }}>
                      {notesResp.notes.voice_notes}
                    </div>
                  </div>
                )}

                {notesResp.notes?.coverage_check && (
                  <div className="p-2 border border-edge bg-surface rounded flex flex-col gap-1">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-signal)" }}>
                      PAST EPISODE COVERAGE
                    </span>
                    <div className="text-xs" style={{ color: "var(--color-phosphor)", whiteSpace: "pre-wrap" }}>
                      {notesResp.notes.coverage_check}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}


/* ------------------------------- STORY SCOUT ------------------------------ */

function StoryScoutTab() {
  const [scoutMode, setScoutMode] = usePersistentState<"pitch" | "image" | "fireside">("tn.scout.mode", "pitch");
  const [query, setQuery]         = usePersistentState("tn.scout.query", "");
  const [category, setCategory]   = usePersistentState("tn.scout.category", "");
  const [days, setDays]           = usePersistentState("tn.scout.days", 7);
  const [exact, setExact]         = usePersistentState("tn.scout.exact", false);
  const [results, setResults]     = usePersistentState<ScoutResult[]>("tn.scout.results", []);
  const [searching, setSearching] = useState(false);
  const [err, setErr]             = useState<string | null>(null);
  const [scoutJobId, setScoutJobId] = useState<string | null>(null);

  const [pitches, setPitches]     = useState<Record<string, { data?: PitchReply; loading?: boolean; err?: string }>>({});

  // IMAGE MODE state
  const [imgUrl, setImgUrl]       = usePersistentState("tn.scout.img_url", "");
  const [scoutImgData, setScoutImgData] = usePersistentState<{ tier1: any[]; tier2: any[]; ai_prompts: string[]; url: string; error?: string } | null>("tn.scout.img_data", null);
  const [scoutImgLoading, setScoutImgLoading] = useState(false);
  const [scoutImgErr, setScoutImgErr] = usePersistentState<string | null>("tn.scout.img_err", null);
  const [copyFeedback, setCopyFeedback] = useState<string | null>(null);

  // WP Media selection & upload state
  const [selected, setSelected]   = useState<Record<string, WpDraft>>({});
  const [wpSending, setWpSending] = useState(false);
  const [wpStatus, setWpStatus]   = useState<Record<string, string>>({});
  const [lastScoutAt, setLastScoutAt] = useState<number>(0);

  const scoutViaClaude = useCallback(async () => {
    const q = query.trim();
    if (!q) { setErr("type a topic first"); return; }
    const cmd = `/f5-story-scout pitch "${q.replace(/["\n\r]/g, "'")}"`;   // no quotes/newlines: insert is type-only, <500 chars
    const r = await post<{ status: string }>("/api/terminal/insert", { text: cmd });
    if (!r.ok) { setErr(r.error || "couldn't reach the terminal — is ttyd/tmux up?"); return; }
    setLastScoutAt(Date.now());
    setErr("Typed into the LIVE terminal. Open the LIVE dock, press Enter to run, wait for it to finish, then click CONVERT.");
  }, [query]);

  const convertFromClaude = useCallback(async () => {
    const res = await fetch("/api/thailandnow/scout/terminal-report");
    if (!res.ok) {
      const d = await res.json().catch(() => ({ detail: res.statusText }));
      setErr(typeof d.detail === "string" ? d.detail : "nothing to convert yet");
      return;
    }
    const data = (await res.json()) as { results: ScoutResult[]; count: number; mtime: number };
    if (lastScoutAt && data.mtime * 1000 < lastScoutAt) {
      setErr(`Handoff is older than your last SCOUT — did the Claude run finish? Showing ${data.count} anyway.`);
    } else {
      setErr(null);
    }
    setResults(data.results);       // persisted (tn.scout.results) + renders existing cards
    setScoutJobId(null);            // no async job — don't leave a poller hanging
  }, [lastScoutAt, setResults]);

  const sendSelectedToWp = useCallback(async () => {
    setWpSending(true);
    for (const [url, draft] of Object.entries(selected)) {
      setWpStatus((s) => ({ ...s, [url]: "sending…" }));
      const r = await post<{ id: number; link: string }>("/api/thailandnow/scout/wp-media", draft);
      const resData = r.data;
      if (r.ok && resData) {
        setWpStatus((s) => ({ ...s, [url]: `✓ #${resData.id}` }));
        setSelected((prev) => {
          const next = { ...prev };
          delete next[url];
          return next;
        });
      } else {
        setWpStatus((s) => ({ ...s, [url]: `err: ${r.error || "upload failed"}` }));
      }
    }
    setWpSending(false);
  }, [selected]);

  const { data: jobsData } = usePolling<{ jobs: TnJob[] }>("/api/thailandnow/jobs", 2000);

  const search = useCallback(async () => {
    setErr(null);
    const r = await post<{ id: string }>("/api/thailandnow/scout/search", { query, category, days, exact });
    if (!r.ok) {
      setErr(r.error?.includes("already running") ? "A search is already running…" : (r.error || "search failed"));
      return;
    }
    setResults([]);
    setScoutJobId(r.data!.id);
    setSearching(true);
  }, [query, category, days, exact, setResults]);

  useEffect(() => {
    if (!scoutJobId || !jobsData?.jobs) return;
    const job = jobsData.jobs.find((j) => j.id === scoutJobId && j.kind === "scout-search");
    if (!job) return;

    if (job.status === "done") {
      fetchJSON<{ results: ScoutResult[] }>(`/api/thailandnow/scout/report/${scoutJobId}`)
        .then((data) => {
          setResults(data.results || []);
          setSearching(false);
          setScoutJobId(null);
        })
        .catch((e) => {
          setErr(String(e));
          setSearching(false);
          setScoutJobId(null);
        });
    } else if (job.status === "error" || job.status === "cancelled") {
      setErr(job.error || `job ${job.status}`);
      setSearching(false);
      setScoutJobId(null);
    }
  }, [scoutJobId, jobsData, setResults]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        search();
      }
    },
    [search]
  );

  const fetchScoutImages = useCallback(async (targetUrl: string) => {
    const cleanUrl = targetUrl.trim();
    if (!cleanUrl) return;
    setScoutImgLoading(true);
    setScoutImgErr(null);
    const r = await post<{ tier1: any[]; tier2: any[]; ai_prompts: string[]; url: string; error?: string }>("/api/thailandnow/scout/images", { url: cleanUrl });
    setScoutImgLoading(false);
    if (r.ok && r.data) {
      setScoutImgData(r.data);
      if (r.data.error) setScoutImgErr(r.data.error);
    } else {
      setScoutImgErr(r.error || "failed to gather images");
    }
  }, []);

  const openImageModeForUrl = useCallback((targetUrl: string) => {
    setImgUrl(targetUrl);
    setScoutMode("image");
    fetchScoutImages(targetUrl);
  }, [fetchScoutImages, setImgUrl, setScoutMode]);

  const makePitch = useCallback(async (resUrl: string) => {
    setPitches((prev) => ({ ...prev, [resUrl]: { loading: true } }));
    const r = await post<PitchReply>("/api/thailandnow/scout/pitch", { url: resUrl });
    if (r.ok && r.data) {
      setPitches((prev) => ({ ...prev, [resUrl]: { data: r.data } }));
    } else {
      setPitches((prev) => ({ ...prev, [resUrl]: { err: r.error || "failed to make pitch" } }));
    }
  }, []);

  return (
    <section className="hud hud--bracket flex flex-col flex-grow reveal reveal-1 p-3">
      {/* Top Header Row with Mode Toggle */}
      <div className="flex items-center justify-between mb-2 shrink-0">
        <span className="label">STORY SCOUT</span>
        <div className="flex items-center gap-1">
          <button
            className={`btn btn--compact ${scoutMode === "pitch" ? "btn--signal" : ""}`}
            onClick={() => setScoutMode("pitch")}
          >
            PITCH MODE
          </button>
          <button
            className={`btn btn--compact ${scoutMode === "image" ? "btn--signal" : ""}`}
            onClick={() => setScoutMode("image")}
          >
            IMAGE MODE
          </button>
          <button
            className={`btn btn--compact ${scoutMode === "fireside" ? "btn--signal" : ""}`}
            onClick={() => setScoutMode("fireside")}
          >
            FIRESIDE MODE
          </button>
        </div>
      </div>

      {scoutMode === "pitch" ? (
        <>
          {/* Pinned Input Row */}
          <div className="flex flex-wrap items-center gap-2 shrink-0 mb-3">
            <input
              className="input"
              style={{ flexGrow: 1, minWidth: 200 }}
              placeholder="topic, exact headline, or paste an article URL"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={handleKeyDown}
            />
            <select
              className="input"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              <option value="">General</option>
              <option value="expat-policy">Expat policy</option>
              <option value="business-investment">Business &amp; investment</option>
              <option value="lifestyle">Lifestyle</option>
            </select>
            <div className="flex items-center gap-2">
              <input
                type="range"
                min={1}
                max={30}
                step={1}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                style={{ width: 120 }}
              />
              <span className="mono text-xs" style={{ color: "var(--color-signal)", minWidth: 50 }}>
                {days} {days === 1 ? "day" : "days"}
              </span>
            </div>
            <label className="mono text-xs flex items-center gap-1" style={{ color: "var(--color-muted)" }}>
              <input type="checkbox" checked={exact} onChange={(e) => setExact(e.target.checked)} />
              Exact article
            </label>
            <button className="btn btn--compact btn--signal" disabled={searching} onClick={search}>
              {searching ? "SEARCHING…" : "SEARCH"}
            </button>
            <button className="btn btn--compact" onClick={scoutViaClaude}>SCOUT ▸ CLAUDE</button>
            <button className="btn btn--compact" onClick={convertFromClaude}>CONVERT ◂ JSON</button>
          </div>
          <div className="mono text-xs mb-3" style={{ color: "var(--color-muted)" }}>
            SCOUT types the command into the LIVE terminal — open the LIVE dock, press Enter there,
            wait for Claude to finish, then CONVERT. (SEARCH is still the instant URL / exact-headline path.)
          </div>

          {/* Scrollable Results Body */}
          <div className="scroll-y flex-grow flex flex-col gap-3">
            {err && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{err}</div>}
            {searching && results.length === 0 && (
              <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>SEARCHING…</div>
            )}
            {!searching && results.length === 0 && !err && (
              <div className="mono" style={{ color: "var(--color-muted)" }}>
                Search for Thailand news to pitch.
              </div>
            )}
            {results.map((r, idx) => {
              const pitchState = pitches[r.url];
              return (
                <div key={r.url || idx} className="border border-edge bg-void p-3 flex flex-col gap-2">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <a
                      href={r.url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-bold hover:underline"
                      style={{ color: "var(--color-signal)" }}
                    >
                      {r.title}
                    </a>
                    <div className="flex items-center gap-2">
                      <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                        {r.source} {r.date ? `· ${r.date}` : ""} {r.lang ? `· ${r.lang.toUpperCase()}` : ""}
                      </span>
                      <button
                        className="btn btn--compact"
                        onClick={() => openImageModeForUrl(r.url)}
                      >
                        FIND IMAGES
                      </button>
                      <button
                        className="btn btn--compact"
                        disabled={pitchState?.loading}
                        onClick={() => makePitch(r.url)}
                      >
                        {pitchState?.loading ? "PITCHING…" : "MAKE PITCH"}
                      </button>
                    </div>
                  </div>

                  {r.snippet && (
                    <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                      {r.snippet}
                    </div>
                  )}

                  {/* Pitch expansion */}
                  {pitchState && (
                    <div className="mt-1 border-t border-edge pt-2 flex flex-col gap-1">
                      {pitchState.err && (
                        <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{pitchState.err}</div>
                      )}
                      {pitchState.data && (
                        pitchState.data.mode === "degraded" ? (
                          <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>
                            LLM gateway unavailable — retry later
                          </div>
                        ) : (
                          <div className="flex flex-col gap-1 bg-surface p-2 rounded">
                            <div className="font-bold text-sm">{pitchState.data.pitch.headline_en}</div>
                            <div className="text-sm" style={{ color: "var(--color-signal)" }}>{pitchState.data.pitch.headline_th}</div>
                            <div className="text-xs italic" style={{ color: "var(--color-muted)" }}>{pitchState.data.pitch.excerpt_en}</div>
                            <div className="mt-1 flex items-center justify-between">
                              <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                                {pitchState.data.mode.toUpperCase()}
                              </span>
                              <button
                                className="btn btn--compact"
                                onClick={() => {
                                  const p = pitchState.data!.pitch;
                                  const block = `${p.headline_en}\n${p.headline_th}\n${p.excerpt_en}`;
                                  navigator.clipboard.writeText(block).catch(() => {});
                                }}
                              >
                                COPY PITCH
                              </button>
                            </div>
                          </div>
                        )
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      ) : scoutMode === "image" ? (
        /* IMAGE MODE Panel */
        <div className="flex flex-col flex-grow">
          {/* Input Row */}
          <div className="flex flex-wrap items-center gap-2 shrink-0 mb-3">
            <input
              className="input"
              style={{ flexGrow: 1, minWidth: 260 }}
              placeholder="Article URL (e.g. https://www.bangkokpost.com/...)"
              value={imgUrl}
              onChange={(e) => setImgUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  fetchScoutImages(imgUrl);
                }
              }}
            />
            <button
              className="btn btn--signal"
              disabled={scoutImgLoading || !imgUrl.trim()}
              onClick={() => fetchScoutImages(imgUrl)}
            >
              {scoutImgLoading ? "FINDING IMAGES…" : "FIND IMAGES"}
            </button>
          </div>

          {/* Results Area */}
          <div className="scroll-y flex-grow flex flex-col gap-4">
            {scoutImgErr && <div className="mono text-xs" style={{ color: "var(--color-critical)" }}>{scoutImgErr}</div>}
            {scoutImgLoading && (
              <div className="mono text-xs" style={{ color: "var(--color-signal)" }}>
                Extracting article images, querying Pexels/Pixabay (≥1080p), and drafting AI prompts…
              </div>
            )}

            {!scoutImgLoading && !scoutImgData && !scoutImgErr && (
              <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                Paste a story URL above (or click FIND IMAGES on any pitch search result) to discover images in 3 tiers.
              </div>
            )}

            {scoutImgData && !scoutImgLoading && (
              <>
                {/* TIER 1 — Article Images */}
                <div className="border border-edge bg-void p-3 flex flex-col gap-2">
                  <div className="flex items-center justify-between">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                      TIER 1 — FROM THE ARTICLE ({scoutImgData.tier1?.length ?? 0})
                    </span>
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>embedded content images</span>
                  </div>
                  {(!scoutImgData.tier1 || scoutImgData.tier1.length === 0) ? (
                    <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>No candidate images extracted from article HTML.</div>
                  ) : (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(140px,1fr))", gap: 8 }}>
                      {scoutImgData.tier1.map((im: any, idx: number) => (
                        <div key={idx} className="border border-edge bg-shade p-1 flex flex-col gap-1">
                          <div className="flex items-center justify-between px-1">
                            <label className="mono text-xs flex items-center gap-1 cursor-pointer" style={{ color: "var(--color-muted)" }}>
                              <input
                                type="checkbox"
                                checked={!!selected[im.url]}
                                onChange={() => {
                                  setSelected((prev) => {
                                    const next = { ...prev };
                                    if (next[im.url]) {
                                      delete next[im.url];
                                    } else {
                                      next[im.url] = wpDefaults(im, 1, scoutImgData.url || imgUrl);
                                    }
                                    return next;
                                  });
                                }}
                              />
                              WP
                            </label>
                            {wpStatus[im.url] && (
                              <span className="mono text-xs font-bold" style={{ color: wpStatus[im.url].startsWith("✓") ? "var(--color-phosphor)" : "var(--color-critical)" }}>
                                {wpStatus[im.url]}
                              </span>
                            )}
                          </div>
                          <a href={im.url} target="_blank" rel="noreferrer" className="block">
                            <img src={im.url} alt={im.alt || "Article visual"} style={{ width: "100%", height: 80, objectFit: "cover" }} />
                          </a>
                          <div className="mono text-xs truncate px-0.5" style={{ color: "var(--color-muted)" }}>
                            {im.alt || bareDomain(im.url)}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* TIER 2 — Stock Imagery */}
                <div className="border border-edge bg-void p-3 flex flex-col gap-2">
                  <div className="flex items-center justify-between">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                      TIER 2 — HIGH-RES STOCK ({scoutImgData.tier2?.length ?? 0})
                    </span>
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>Pexels / Pixabay · ≥1080p</span>
                  </div>
                  {(!scoutImgData.tier2 || scoutImgData.tier2.length === 0) ? (
                    <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>No high-res stock matches found.</div>
                  ) : (
                    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(140px,1fr))", gap: 8 }}>
                      {scoutImgData.tier2.map((im: any, idx: number) => (
                        <div key={idx} className="border border-edge bg-shade p-1 flex flex-col gap-1">
                          <div className="flex items-center justify-between px-1">
                            <label className="mono text-xs flex items-center gap-1 cursor-pointer" style={{ color: "var(--color-muted)" }}>
                              <input
                                type="checkbox"
                                checked={!!selected[im.url]}
                                onChange={() => {
                                  setSelected((prev) => {
                                    const next = { ...prev };
                                    if (next[im.url]) {
                                      delete next[im.url];
                                    } else {
                                      next[im.url] = wpDefaults(im, 2, scoutImgData.url || imgUrl);
                                    }
                                    return next;
                                  });
                                }}
                              />
                              WP
                            </label>
                            {wpStatus[im.url] && (
                              <span className="mono text-xs font-bold" style={{ color: wpStatus[im.url].startsWith("✓") ? "var(--color-phosphor)" : "var(--color-critical)" }}>
                                {wpStatus[im.url]}
                              </span>
                            )}
                          </div>
                          <a href={im.url} target="_blank" rel="noreferrer" className="block">
                            <img src={im.thumb || im.url} alt="Stock option" style={{ width: "100%", height: 80, objectFit: "cover" }} />
                          </a>
                          <div className="flex items-center justify-between mono text-xs px-0.5">
                            <span className="font-bold" style={{ color: "var(--color-signal)" }}>{im.provider?.toUpperCase()}</span>
                            <span style={{ color: "var(--color-muted)" }}>{im.w}x{im.h}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* TIER 3 — AI Prompts */}
                <div className="border border-edge bg-void p-3 flex flex-col gap-2">
                  <div className="flex items-center justify-between">
                    <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                      TIER 3 — AI PROMPTS ({scoutImgData.ai_prompts?.length ?? 0})
                    </span>
                    {scoutImgData.ai_prompts && scoutImgData.ai_prompts.length > 0 && (
                      <button
                        className="btn btn--compact"
                        onClick={() => {
                          navigator.clipboard.writeText(scoutImgData.ai_prompts.join("\n\n"));
                          setCopyFeedback("Copied all!");
                          setTimeout(() => setCopyFeedback(null), 2000);
                        }}
                      >
                        {copyFeedback === "Copied all!" ? "✓ COPIED ALL" : "COPY ALL PROMPTS"}
                      </button>
                    )}
                  </div>
                  <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                    Prompts for Google Flow / Gemini Image Generator (paid sub). Run generation manually in Flow or Imagen.
                  </div>
                  {(!scoutImgData.ai_prompts || scoutImgData.ai_prompts.length === 0) ? (
                    <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>No AI prompts generated.</div>
                  ) : (
                    <div className="flex flex-col gap-2 mt-1">
                      {scoutImgData.ai_prompts.map((promptStr: string, pIdx: number) => (
                        <div key={pIdx} className="p-2 border border-edge bg-surface flex flex-col gap-1 rounded">
                          <div className="text-xs mono" style={{ color: "var(--color-phosphor)" }}>{promptStr}</div>
                          <div className="flex justify-end">
                            <button
                              className="btn btn--compact"
                              onClick={() => {
                                navigator.clipboard.writeText(promptStr);
                                setCopyFeedback(`Copied #${pIdx + 1}`);
                                setTimeout(() => setCopyFeedback(null), 2000);
                              }}
                            >
                              {copyFeedback === `Copied #${pIdx + 1}` ? "✓ COPIED" : "COPY PROMPT"}
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* REVIEW AND SEND TO WP */}
                {Object.keys(selected).length > 0 && (
                  <div className="border border-signal bg-shade p-3 flex flex-col gap-3 shrink-0">
                    <div className="flex items-center justify-between">
                      <span className="mono text-xs font-bold" style={{ color: "var(--color-phosphor)" }}>
                        SEND TO WORDPRESS MEDIA LIBRARY ({Object.keys(selected).length} selected)
                      </span>
                      <button className="btn btn--signal" disabled={wpSending} onClick={sendSelectedToWp}>
                        {wpSending ? "SENDING…" : `SEND ${Object.keys(selected).length} TO WP`}
                      </button>
                    </div>
                    <div className="flex flex-col gap-3 max-h-60 overflow-y-auto pr-1">
                      {Object.entries(selected).map(([url, draft]) => (
                        <div key={url} className="flex gap-2 p-2 border border-edge bg-void text-xs">
                          <img src={url} alt="" style={{ width: 60, height: 60, objectFit: "cover" }} className="shrink-0" />
                          <div className="flex flex-col gap-1 flex-grow">
                            <div className="flex items-center gap-2">
                              <span className="mono font-bold w-16 shrink-0" style={{ color: "var(--color-muted)" }}>Title:</span>
                              <input
                                className="input text-xs flex-grow"
                                value={draft.title}
                                onChange={(e) => {
                                  const val = e.target.value;
                                  setSelected((prev) => ({ ...prev, [url]: { ...prev[url], title: val } }));
                                }}
                              />
                            </div>
                            <div className="flex items-center gap-2">
                              <span className="mono font-bold w-16 shrink-0" style={{ color: "var(--color-muted)" }}>Alt text:</span>
                              <input
                                className="input text-xs flex-grow"
                                value={draft.alt_text}
                                onChange={(e) => {
                                  const val = e.target.value;
                                  setSelected((prev) => ({ ...prev, [url]: { ...prev[url], alt_text: val } }));
                                }}
                              />
                            </div>
                            <div className="flex items-center gap-2">
                              <span className="mono font-bold w-16 shrink-0" style={{ color: "var(--color-muted)" }}>Caption:</span>
                              <input
                                className="input text-xs flex-grow"
                                value={draft.caption}
                                onChange={(e) => {
                                  const val = e.target.value;
                                  setSelected((prev) => ({ ...prev, [url]: { ...prev[url], caption: val } }));
                                }}
                              />
                            </div>
                          </div>
                          {wpStatus[url] && (
                            <div className="mono text-xs shrink-0 self-center font-bold px-1" style={{ color: wpStatus[url].startsWith("✓") ? "var(--color-phosphor)" : "var(--color-critical)" }}>
                              {wpStatus[url]}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      ) : (
        /* FIRESIDE MODE Panel */
        <FiresidePanel />
      )}
    </section>
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

/* ------------------------------ WORDPRESS OP ------------------------------ */

interface TrelloToPublishCard {
  id: string;
  name: string;
}

interface ToPublishResp {
  cards: TrelloToPublishCard[];
}

interface AnalyzeCardResp {
  card_id: string;
  title: string;
  kind?: string;
  location?: string;
  dates_raw?: string;
  start_date?: string;
  end_date?: string;
  doc_text: string;
  seo_model: string;
  seo: {
    keyphrases: string[];
    metas: string[];
    hashtags: string;
    ai_a: string;
    ai_b: string[];
    focus_keyphrase?: string;
    meta_description?: string;
  };
}

interface PublishFromCardResp {
  wp_id: number;
  link: string;
  status: string;
  kind?: string;
  title?: string;
  location?: string;
  dates_raw?: string;
  start_date?: string;
  end_date?: string;
  seo_model: string;
  images_uploaded: number;
  seo: {
    keyphrases: string[];
    metas: string[];
    hashtags: string;
    ai_a: string;
    ai_b: string[];
    focus_keyphrase?: string;
    meta_description?: string;
    best_keyphrase?: string;
    best_meta?: string;
    key_takeaways?: string[];
  };
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // ignore
    }
  };

  return (
    <button type="button" className="btn btn--compact" onClick={handleCopy}>
      {copied ? "COPIED!" : "COPY"}
    </button>
  );
}

function WpOpTab() {
  const [mode, setMode] = useState<"articles" | "events" | "blogs">("articles");
  const [cards, setCards] = useState<TrelloToPublishCard[]>([]);
  const [loadingCards, setLoadingCards] = useState(false);
  const [cardsErr, setCardsErr] = useState<string | null>(null);

  const [selectedCardId, setSelectedCardId] = useState<string | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeErr, setAnalyzeErr] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<AnalyzeCardResp | null>(null);

  const [publishing, setPublishing] = useState(false);
  const [publishErr, setPublishErr] = useState<string | null>(null);
  const [publishResult, setPublishResult] = useState<PublishFromCardResp | null>(null);

  const loadCards = useCallback(async () => {
    setLoadingCards(true);
    setCardsErr(null);
    try {
      const res = await fetchJSON<ToPublishResp>("/api/thailandnow/events/to-publish");
      setCards(res.cards || []);
    } catch (e) {
      setCardsErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingCards(false);
    }
  }, []);

  useEffect(() => {
    void loadCards();
  }, [loadCards]);

  const handleAnalyze = async (cardId: string) => {
    setSelectedCardId(cardId);
    setAnalyzing(true);
    setAnalyzeErr(null);
    setAnalysis(null);
    setPublishing(false);
    setPublishErr(null);
    setPublishResult(null);

    const r = await post<AnalyzeCardResp>("/api/thailandnow/events/analyze-card", {
      card_id: cardId,
      kind: mode === "blogs" ? "blog" : mode === "articles" ? "article" : "event",
    });

    setAnalyzing(false);
    if (r.ok && r.data) {
      setAnalysis(r.data);
    } else {
      setAnalyzeErr(r.error ?? "Card analysis failed");
    }
  };

  const handlePublishFromCard = async () => {
    if (!analysis) return;
    setPublishing(true);
    setPublishErr(null);

    const r = await post<PublishFromCardResp>("/api/thailandnow/events/publish-from-card", {
      card_id: analysis.card_id,
      kind: mode === "blogs" ? "blog" : mode === "articles" ? "article" : "event",
    });

    setPublishing(false);
    if (r.ok && r.data) {
      setPublishResult(r.data);
    } else {
      setPublishErr(r.error ?? "Publish from card failed");
    }
  };

  return (
    <>
      <section className="hud hud--bracket reveal reveal-1 p-3 flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="label">WORDPRESS OP · TO PUBLISH (NAZ + TOON)</div>
          <button
            type="button"
            className="btn btn--compact"
            disabled={loadingCards}
            onClick={() => void loadCards()}
          >
            {loadingCards ? "REFRESHING…" : "REFRESH"}
          </button>
        </div>

        {/* Mode Selector: Articles vs Events */}
        <div className="flex items-center gap-2 mt-1">
          <span className="label">POST TYPE:</span>
          <button
            type="button"
            className={`btn btn--compact ${mode === "articles" ? "btn--signal font-bold" : ""}`}
            onClick={() => {
              setMode("articles");
              setSelectedCardId(null);
              setAnalysis(null);
              setPublishResult(null);
            }}
          >
            📝 Articles (/posts)
          </button>
          <button
            type="button"
            className={`btn btn--compact ${mode === "events" ? "btn--signal font-bold" : ""}`}
            onClick={() => {
              setMode("events");
              setSelectedCardId(null);
              setAnalysis(null);
              setPublishResult(null);
            }}
          >
            🎪 Events (/event)
          </button>
          <button
            type="button"
            className={`btn btn--compact ${mode === "blogs" ? "btn--signal font-bold" : ""}`}
            onClick={() => {
              setMode("blogs");
              setSelectedCardId(null);
              setAnalysis(null);
              setPublishResult(null);
            }}
          >
            ✍️ Blogs (/posts)
          </button>
        </div>

        <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
          {mode === "articles"
            ? "Draft standard WordPress Articles (/posts) with Key Takeaways and image spacers."
            : mode === "blogs"
              ? "Draft Blogs (/posts) — title from the doc's first line, NO Key Takeaways, images only if present."
              : "Draft Event custom posts (/event) with Key Takeaways, dates, location, and images."}
        </div>

        {loadingCards && (
          <div className="mono caret mt-1" style={{ color: "var(--color-signal)" }}>
            FETCHING CARDS FROM TRELLO…
          </div>
        )}

        {cardsErr && (
          <div className="mt-1">
            <ErrLine msg={cardsErr} />
          </div>
        )}

        {!loadingCards && !cardsErr && cards.length === 0 && (
          <div className="mono mt-1" style={{ color: "var(--color-muted)" }}>
            No cards found in &apos;To publish (NAZ + TOON)&apos; Trello list.
          </div>
        )}

        {cards.length > 0 && (
          <div className="scroll-y flex flex-col gap-1 mt-1" style={{ maxHeight: 240 }}>
            {cards.map((c) => {
              const isSelected = selectedCardId === c.id;
              const isThisAnalyzing = isSelected && analyzing;
              return (
                <div key={c.id} className="row-in flex items-center justify-between gap-2 p-2">
                  <div className="flex items-center gap-2 overflow-hidden">
                    <span className={`pip ${isSelected ? "pip--go" : "pip--signal"}`} />
                    <span
                      className="mono text-sm truncate font-bold"
                      style={{ color: isSelected ? "var(--color-signal)" : "var(--color-phosphor)" }}
                    >
                      {c.name}
                    </span>
                  </div>
                  <button
                    type="button"
                    className={`btn btn--compact ${isSelected ? "btn--armed" : "btn--signal"}`}
                    disabled={analyzing}
                    onClick={() => void handleAnalyze(c.id)}
                  >
                    {isThisAnalyzing ? "ANALYZING…" : "ANALYZE"}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {analyzing && (
        <section className="hud hud--bracket reveal reveal-2 p-3">
          <div className="mono caret" style={{ color: "var(--color-signal)" }}>
            ANALYZING CARD &amp; GENERATING SEO… ~1–2 min (fetching doc + generating SEO)
          </div>
        </section>
      )}

      {analyzeErr && (
        <section className="hud hud--bracket reveal reveal-2 p-3">
          <ErrLine msg={analyzeErr} />
        </section>
      )}

      {analysis && !analyzing && (
        <section className="hud hud--bracket reveal reveal-2 flex flex-col gap-3 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <div className="label">ANALYSIS · {analysis.title}</div>
              {publishResult ? (
                <div className="flex items-center gap-2">
                  <span className="pip pip--go" />
                  <span className="mono font-bold text-xs" style={{ color: "var(--color-go)" }}>
                    done ✓
                  </span>
                  <a
                    href={publishResult.link}
                    target="_blank"
                    rel="noreferrer"
                    className="btn btn--compact btn--go text-xs font-bold"
                  >
                    OPEN DRAFT #{publishResult.wp_id} ↗
                  </a>
                  {publishResult.images_uploaded > 0 && (
                    <span className="mono text-xs" style={{ color: "var(--color-muted)" }}>
                      (Images uploaded: {publishResult.images_uploaded})
                    </span>
                  )}
                </div>
              ) : (
                <button
                  type="button"
                  className="btn btn--signal btn--compact font-bold"
                  disabled={publishing}
                  onClick={() => void handlePublishFromCard()}
                >
                  {publishing
                    ? "PUBLISHING DRAFT…"
                    : `Publish ${mode === "articles" ? "Article" : mode === "blogs" ? "Blog" : "Event"} to WordPress (${mode === "events" ? "/event" : "/posts"})`}
                </button>
              )}
            </div>
            <div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
              Model: {analysis.seo_model} (Card ID: #{analysis.card_id})
            </div>
          </div>

          {publishErr && (
            <div className="mt-1">
              <ErrLine msg={publishErr} />
            </div>
          )}

          {/* WordPress Event Inputs (Only shown for Events) */}
          {mode === "events" && ((analysis.location || publishResult?.location) || (analysis.dates_raw || publishResult?.dates_raw)) && (
            <div className="flex flex-col gap-2 p-2 border border-edge bg-void rounded">
              <div className="label">WORDPRESS EVENT INPUTS</div>

              {/* Event Place */}
              {(publishResult?.location || analysis.location) && (
                <div className="row-in flex items-center justify-between gap-2 p-2">
                  <div className="flex flex-col gap-0.5 overflow-hidden">
                    <span className="mono text-xs" style={{ color: "var(--color-signal)" }}>
                      EVENT PLACE
                    </span>
                    <span className="mono font-bold" style={{ color: "var(--color-phosphor)" }}>
                      {publishResult?.location || analysis.location}
                    </span>
                  </div>
                  <CopyButton text={publishResult?.location || analysis.location || ""} />
                </div>
              )}

              {/* Start & End Date */}
              {((publishResult?.start_date || analysis.start_date) || (publishResult?.dates_raw || analysis.dates_raw)) && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  <div className="row-in flex items-center justify-between gap-2 p-2">
                    <div className="flex flex-col gap-0.5 overflow-hidden">
                      <span className="mono text-xs" style={{ color: "var(--color-signal)" }}>
                        START DATE {publishResult?.dates_raw || analysis.dates_raw ? `(${publishResult?.dates_raw || analysis.dates_raw})` : ""}
                      </span>
                      <span className="mono font-bold" style={{ color: "var(--color-phosphor)" }}>
                        {publishResult?.start_date || analysis.start_date || "—"}
                      </span>
                    </div>
                    <CopyButton text={publishResult?.start_date || analysis.start_date || ""} />
                  </div>

                  <div className="row-in flex items-center justify-between gap-2 p-2">
                    <div className="flex flex-col gap-0.5 overflow-hidden">
                      <span className="mono text-xs" style={{ color: "var(--color-signal)" }}>
                        END DATE
                      </span>
                      <span className="mono font-bold" style={{ color: "var(--color-phosphor)" }}>
                        {publishResult?.end_date || analysis.end_date || "—"}
                      </span>
                    </div>
                    <CopyButton text={publishResult?.end_date || analysis.end_date || ""} />
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Google Doc Text */}
          <div className="border border-edge bg-void p-2">
            <div className="label mb-1">GOOGLE DOC TEXT</div>
            <pre
              className="mono scroll-y p-2 text-xs"
              style={{
                maxHeight: 260,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                color: "var(--color-phosphor-dim)",
                background: "var(--color-surface-2, rgba(0,0,0,0.18))",
                borderRadius: 4,
              }}
            >
              {analysis.doc_text}
            </pre>
          </div>

          <div className="label mt-1">SEO OUTPUTS (MANUAL YOAST PASTE)</div>

          {/* Focus Keyphrases */}
          <div className="flex flex-col gap-1">
            <div className="label">FOCUS KEYPHRASES (5)</div>
            {(publishResult?.seo?.keyphrases || analysis.seo?.keyphrases || []).map((kp, idx) => {
              const isBest =
                publishResult?.seo?.focus_keyphrase === kp ||
                publishResult?.seo?.best_keyphrase === kp;
              return (
                <div
                  key={idx}
                  className="row-in flex items-center justify-between gap-2 p-2"
                  style={
                    isBest
                      ? { border: "1px solid var(--color-go)", background: "rgba(0, 255, 128, 0.08)" }
                      : undefined
                  }
                >
                  <div className="flex items-center gap-2 overflow-hidden">
                    {isBest && (
                      <span
                        className="mono text-xs font-bold px-1.5 py-0.5 rounded"
                        style={{ background: "var(--color-go)", color: "#000" }}
                      >
                        BEST KEYPHRASE
                      </span>
                    )}
                    <span
                      className="mono"
                      style={{ color: isBest ? "var(--color-go)" : "var(--color-phosphor-dim)" }}
                    >
                      {kp}
                    </span>
                  </div>
                  <CopyButton text={kp} />
                </div>
              );
            })}
          </div>

          {/* Meta Descriptions */}
          <div className="flex flex-col gap-1">
            <div className="label">META DESCRIPTIONS (5)</div>
            {(publishResult?.seo?.metas || analysis.seo?.metas || []).map((meta, idx) => {
              const isBest =
                publishResult?.seo?.meta_description === meta ||
                publishResult?.seo?.best_meta === meta;
              return (
                <div
                  key={idx}
                  className="row-in flex items-center justify-between gap-2 p-2"
                  style={
                    isBest
                      ? { border: "1px solid var(--color-go)", background: "rgba(0, 255, 128, 0.08)" }
                      : undefined
                  }
                >
                  <div className="flex items-center gap-2 overflow-hidden">
                    {isBest && (
                      <span
                        className="mono text-xs font-bold px-1.5 py-0.5 rounded"
                        style={{ background: "var(--color-go)", color: "#000" }}
                      >
                        BEST META
                      </span>
                    )}
                    <span
                      className="mono"
                      style={{ color: isBest ? "var(--color-go)" : "var(--color-phosphor-dim)" }}
                    >
                      {meta}
                    </span>
                  </div>
                  <CopyButton text={meta} />
                </div>
              );
            })}
          </div>

          {/* Hashtags */}
          <div className="row-in flex flex-col gap-1 p-2">
            <div className="flex items-center justify-between">
              <span className="label">HASHTAGS</span>
              <CopyButton text={analysis.seo?.hashtags || ""} />
            </div>
            <div className="mono" style={{ color: "var(--color-phosphor-dim)" }}>
              {analysis.seo?.hashtags}
            </div>
          </div>

          {/* Version A */}
          <div className="row-in flex flex-col gap-1 p-2">
            <div className="flex items-center justify-between">
              <span className="label">VERSION A (AI SUMMARY)</span>
              <CopyButton text={analysis.seo?.ai_a || ""} />
            </div>
            <div className="mono" style={{ color: "var(--color-phosphor-dim)", whiteSpace: "pre-wrap" }}>
              {analysis.seo?.ai_a}
            </div>
          </div>

          {/* Version B */}
          <div className="row-in flex flex-col gap-1 p-2">
            <div className="flex items-center justify-between">
              <span className="label">VERSION B (KEY TAKEAWAYS)</span>
              <CopyButton text={(analysis.seo?.ai_b || []).map((b) => `• ${b}`).join("\n")} />
            </div>
            <div className="mono flex flex-col gap-1" style={{ color: "var(--color-phosphor-dim)" }}>
              {(analysis.seo?.ai_b || []).map((item, idx) => (
                <div key={idx}>• {item}</div>
              ))}
            </div>
          </div>
        </section>
      )}
    </>
  );
}
