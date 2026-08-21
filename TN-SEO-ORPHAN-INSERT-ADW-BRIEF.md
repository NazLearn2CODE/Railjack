# BRIEF — Thailand NOW SEO: un-orphan via analyzed anchor-link insertion

Railjack (FastAPI hub + React cockpit). Target repo: `/var/home/NAZ/Coding Projects/Railjack`.
You are a single-turn builder: implement EXACTLY this brief, no extras, no formatting churn
outside touched regions. Follow existing code idioms in each file. When done, run nothing —
the host gates you.

## Feature (settled design — do not reinterpret)

The SEO HEALTH tab lists ORPHAN ARTICLES (zero inbound internal links). Today the only
remedy is the ✎ manual WP editor. New flow, **inbound direction** (the host article gets a
link TO the orphan — that is what clears orphan status):

1. Clicking an orphan row expands it; each of its existing `suggested` articles gets an
   **ANALYZE** button.
2. ANALYZE → backend reads that host article's raw content and returns **anchor
   candidates**: phrases derived from the orphan's title that occur at a *valid* spot in
   the host. Only phrases with ≥1 valid occurrence, ranked longest-first, capped at 8.
3. Clicking a candidate → **preview** (before/after HTML snippets, same visual pattern as
   the existing removal PreviewBlock) → **CONFIRM INSERT** → backend wraps the FIRST valid
   occurrence as a bare `<a href="{orphan_link}">phrase</a>` and PUTs the content.
4. After a successful insert (matches > 0) the orphan is optimistically removed from the
   client-side report list. No auto-rescan.

Deterministic, pure, Thai-safe matching (no word segmentation, no LLM). No new deps.

## Context anchors (read these first)

- `app/thailandnow.py` (~5970 lines), SEO sub-module block starting ~3644:
  - `_SEO_STOP` stopword set (~3660); `_seo_tokens` (~3934); `_seo_suggest` (~3939).
  - `_wp_creds` (~3677), `_wp` async REST helper (~3720), `_wp_resolve_rest_base` (~3753),
    `_wp_site_host` (~3713), `_seo_classify` (~3888).
  - `_seo_strip_target` (~4301) — the signature pattern to mirror:
    `(html, ...) -> tuple[str, int, str, str]` returning `(new_html, matches, snippet_before,
    snippet_after)` with ~40 chars context around the first match.
  - Existing endpoints: `POST /api/thailandnow/seo/scan` (~4379), `/seo/report/{jid}` (~4389),
    `/seo/preview-fix` (~4400), `/seo/apply-fix` (~4427), `/seo/apply-fix-bulk` (~4462).
    Request models `SeoFixReq` / `SeoApplyFixBulkReq` (~4369). Note the permission-error
    handling idiom in preview-fix (401/403 → HTTPException 403 with app-password hint).
  - Module `__main__` self-check block: SEO asserts live at ~5764-5850 (pure-function,
    assert-based). Extend that block — do NOT create a new test file.
- `frontend/src/components/ThailandNowPanel.tsx` (~3960 lines), `HealthSubTab`:
  - `HealthOrphan` interface (~168): `{ id?, link, title, suggested: HealthSuggestion[] }`.
  - `healthCopyText` (~253-281) builds the COPY export.
  - Preview/remove machinery: `ActivePreview` state + `handleStartPreview` (~530),
    `handleApplyFix` (~543), `PreviewBlock` component (renders loading / error / zero-match
    / before-after `<pre>` blocks, buttons CONFIRM REMOVE + CANCEL — the label is currently
    hardcoded), `trimFixed` + optimistic `setReport` trim idiom.
  - ORPHAN ARTICLES `HealthList` render at ~614-635: title link, `WpEdit` button, then the
    " — link with: " suggested-links span. This is the row to extend.
  - `post` / `fetchJSON` helpers already in file.

## Build — backend (`app/thailandnow.py`)

1. `_seo_valid_regions(html: str) -> list[tuple[int, int]]` — pure helper: index ranges of
   `html` that are OUTSIDE `<a>...</a>`, `<h1>`–`<h6>` headings, `<pre>...</pre>`, and
   `<code>...</code>` blocks (case-insensitive, DOTALL; nested-tag paranoia not required —
   flat scan of those four tag families is enough for WP content).
2. `_seo_anchor_phrases(orphan_title: str) -> list[str]` — pure: candidate phrases from the
   orphan title = the full whitespace-normalized title, plus every contiguous token n-gram
   of length ≥ 2 (tokens = whitespace split; Thai titles are typically one long token, so
   the full title already covers Thai). Drop phrases that are stopword-only after filtering
   with `_SEO_STOP`, drop phrases shorter than 4 chars, dedup, sort longest-first.
3. `_seo_anchor_candidates(orphan_title: str, host_html: str, cap: int = 8) -> list[dict]` —
   pure: for each phrase from `_seo_anchor_phrases`, count case-insensitive occurrences at
   valid positions (`_seo_valid_regions`); keep phrases with ≥1 valid occurrence; return
   `[{"phrase": str, "count": int, "snippet": str}]` (~60-char context around the first
   valid occurrence), longest phrase first, capped at `cap`.
4. `_seo_insert_link(html: str, phrase: str, href: str) -> tuple[str, int, str, str]` —
   pure, mirrors `_seo_strip_target`: find the FIRST case-insensitive occurrence of
   `phrase` at a valid position, wrap it as `<a href="{href}">{original_text}</a>`
   (preserve the original casing of the matched text, escape nothing else), return
   `(new_html, matches, snippet_before, snippet_after)` with the same ~40-char context
   convention. 0 valid occurrences → `(html, 0, "", "")`.
5. Request models beside `SeoFixReq`:
   - `class SeoAnchorAnalyzeReq(BaseModel): host_id: int; orphan_title: str; orphan_link: str`
   - `class SeoInsertReq(BaseModel): host_id: int; phrase: str; href: str`
6. Endpoints beside the existing seo ones (same read idiom: `_wp_resolve_rest_base`,
   GET `/{rb}/{id}?context=edit`, same 401/403 → 403 hint, 404 shape):
   - `POST /api/thailandnow/seo/analyze-anchors` — validate `orphan_link` is internal
     (`_seo_classify(orphan_link, _wp_site_host()) == "internal"`, else 400), read host
     raw content, return `{"host_id", "orphan_title", "orphan_link", "candidates": [...]}`.
     No WP write.
   - `POST /api/thailandnow/seo/preview-insert` — validate `href` internal (same check,
     400 on failure), read host raw, run `_seo_insert_link`, return
     `{"host_id", "phrase", "href", "matches", "before", "after"}`. No WP write.
   - `POST /api/thailandnow/seo/apply-insert` — same read; if matches == 0 return
     `{"ok": True, "matches": 0, "post_id", "post_link"}` without writing (idempotent);
     else `POST /{rb}/{host_id}` `{"content": new_html}` (mirror apply-fix) and return
     `{"ok": True, "matches", "post_id", "post_link"}`.
7. Self-check asserts in the `__main__` block beside the existing SEO ones (~5764+):
   - `_seo_valid_regions`: a phrase inside an `<a>` inner text and inside an `<h2>` is
     invalid; plain-paragraph occurrence is valid.
   - `_seo_anchor_phrases`: English title ("Khon Kaen Street Food Guide") drops
     stopword-only n-grams and yields longest-first list; Thai title (e.g.
     "เที่ยวขอนแก่น 3 วัน") yields the full title as a candidate.
   - `_seo_anchor_candidates`: returns only occurring phrases, longest-first, cap honored,
     snippet non-empty.
   - `_seo_insert_link`: wraps the first valid occurrence preserving original text casing;
     a second occurrence stays unwrapped when the first is inside an `<a>` (i.e. matches==1
     and the wrapped one is the valid one); 0-match input returns matches 0 unchanged.

## Build — frontend (`frontend/src/components/ThailandNowPanel.tsx`)

1. Generalize `PreviewBlock` minimally: optional `confirmLabel?: string` prop defaulting
   to `"CONFIRM REMOVE"`; pass `"CONFIRM INSERT"` from the new flow. No other changes.
2. `HealthSubTab` new state (follow existing useState idioms):
   - `expandedOrphan: string | null` (the orphan `link`).
   - `anchorData: { hostId: number; orphanLink: string; loading: boolean; error?: string;
     candidates: { phrase: string; count: number; snippet: string }[] } | null`
   - `insertPreview: { key: string; hostId: number; phrase: string; href: string; loading:
     boolean; data?: { matches: number; before: string; after: string }; error?: string;
     applied?: boolean } | null`
   - handlers `handleAnalyze(hostId, orphanTitle, orphanLink)`, `handlePreviewInsert(key,
     hostId, phrase, href)`, `handleApplyInsert()` mirroring the existing
     start-preview/apply-fix patterns; on apply success with matches > 0 optimistically
     `setReport` the orphan OUT of `report.orphans` (keep everything else untouched) and
     clear insert/anchor state.
3. ORPHAN ARTICLES rows: make the title `<a>` click ALSO toggle `expandedOrphan` (keep
   `target="_blank"` on an explicit open-in-tab affordance OR toggle on a small ▸ button —
   pick whichever is the smaller diff, but clicking the orphan title must expand the row).
   When expanded, under the row render for EACH `o.suggested` entry a compact line:
   `▸ {s.title} [ANALYZE]`; when `anchorData` is for that host, render the candidate list:
   each candidate `"{phrase} ×{count}"` as a button (mono, small) that starts the insert
   preview; below it the snippet in muted mono text-xs. Render `PreviewBlock` for the
   insert flow with `confirmLabel="CONFIRM INSERT"`, `onApply=handleApplyInsert`.
   Loading/error states mirrored from the removal flow.
4. Update the ORPHAN ARTICLES `hint` to mention the new flow (one clause, keep it terse:
   e.g. "click an article to analyze where to embed an inbound link").
5. No COPY-format changes, no new components beyond what's above, no styling-system
   changes (reuse `mono text-xs`, `var(--color-muted)`, `btn btn--compact` idioms).

## Hard constraints

- No new dependencies anywhere. No network calls in pure helpers. All WP I/O goes through
  the existing `_wp` helper. Never write WP in analyze/preview endpoints.
- `href`/`orphan_link` internal-site validation is a security guard — never skip it.
- Keep diffs surgical; do not reformat untouched regions. No changes to other tabs,
  endpoints, or files (except the two named files).
- Idempotency: apply-insert with 0 matches must not PUT/POST to WP.

## Report (return exactly this shape)

```
FILES CHANGED:
- app/thailandnow.py — <one line per addition: helpers, models, endpoints, asserts>
- frontend/src/components/ThailandNowPanel.tsx — <one line per addition>
NOTES: <anything ambiguous you had to decide, or "none">
```
