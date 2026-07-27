# STORY SCOUT — refinement plan (build spec for Antigravity IDE)

**Status:** Slices 0–3 SHIPPED + Tawhan-verified 2026-07-27 (`ef36df0` persist, `cf904eb`
lookup, `a21d1db` send-to-WP). **Slice 4 (terminal discovery) = NEXT, planned below.**
**Build:** Antigravity IDE · **Verify:** Tawhan
**Module:** THAILAND NOW → STORY SCOUT tab
**Files in scope:**
- Backend: `app/thailandnow.py`
- Frontend: `frontend/src/components/ThailandNowPanel.tsx` (component `StoryScoutTab`)
- **Skill (OUTSIDE the repo):** `/home/NAZ/.claude/skills/f5-story-scout/SKILL.md` (Slice 4 only)

This is an *implement-not-copy* guide. Anchors (`file:line`) drift as slices land — as of
Slice 3, `StoryScoutTab` starts at `:1518`, the pitch input row at `:1666`, the card `.map`
at `:1719`, `ScoutResult` type at `:123`, `post`/`usePersistentState` at `:166`/`:195`.
**Match on quoted symbol names, not numbers.** Naz is a non-coder — every slice ends with a
**VERIFY** block that must pass before moving on.

---

## Slice 0 — #3 is ALREADY DONE (verify only, no build)

Requirement #3 ("FIND IMAGES on a pitch result redirects to IMAGE MODE with the URL
prefilled") is **already implemented**. Do not rebuild it.

- `openImageModeForUrl()` — `ThailandNowPanel.tsx:1579` — sets `imgUrl`, flips
  `scoutMode` to `"image"`, and auto-fetches.
- Wired to the FIND IMAGES button on each pitch result at `:1688`.

**VERIFY:** In PITCH MODE, run a search, click FIND IMAGES on a result → the view flips
to IMAGE MODE with that URL in the input box and images loading. If that already works,
Slice 0 is complete.

---

## Slice 1 — #1 Persist IMAGE MODE results until the next scan

**Problem:** `imgUrl` persists (`usePersistentState`, `:1510`) but the *results*
(`scoutImgData`, `scoutImgErr`) are plain `useState` (`:1511`, `:1513`), so they vanish
on tab switch / reload. The input survives; the results don't.

**Change — `ThailandNowPanel.tsx`, inside `StoryScoutTab` (~L1511–1513):**

```ts
// BEFORE
const [scoutImgData, setScoutImgData] = useState<{ tier1: any[]; tier2: any[]; ai_prompts: string[]; url: string; error?: string } | null>(null);
const [scoutImgLoading, setScoutImgLoading] = useState(false);
const [scoutImgErr, setScoutImgErr] = useState<string | null>(null);

// AFTER
const [scoutImgData, setScoutImgData] = usePersistentState<{ tier1: any[]; tier2: any[]; ai_prompts: string[]; url: string; error?: string } | null>("tn.scout.img_data", null);
const [scoutImgLoading, setScoutImgLoading] = useState(false); // transient — leave as useState
const [scoutImgErr, setScoutImgErr] = usePersistentState<string | null>("tn.scout.img_err", null);
```

- `usePersistentState` already exists (`:174`) — same signature as `useState` plus a
  localStorage key. No import change.
- `scoutImgLoading` **stays** plain `useState` — a spinner should never survive a reload.
- `fetchScoutImages` (`:1564`) already overwrites `scoutImgData` on the next scan, so
  "persist until next scan" falls out for free. No other change needed.

**VERIFY:** IMAGE MODE → FIND IMAGES → wait for 3 tiers → switch to another THAILAND NOW
sub-tab and back, then hard-reload the page. The tiers are still there. Running FIND
IMAGES again replaces them.

---

## Slice 2 — #2a Fix pitch search: add a LOOKUP path (the cheap, correct fix)

**Root cause of "I pasted the exact title but got adjacent stories":** `_scout_news`
(`app/thailandnow.py:990`) is a *discovery* sweep, not a *lookup*. Three choices break
exact-title / exact-URL intent:

1. **Domain dedup** (`:1057`) — keeps only ONE url per domain. The exact article and its
   neighbours share a domain; whichever the engine returns first wins.
2. **Discovery scaffolding** (`:1014`) — wraps input in `Thailand {q} news {Month Year}`
   + a Thai translation + a `site:` filter. Dilutes an exact headline.
3. **Strict date drop** (`:1077`) — undated / unparseable-date articles are discarded.

### Backend changes — `app/thailandnow.py`

**2.1 — URL detector helper** (near the other `_scout_*` helpers, ~L881):

```python
def _looks_like_url(s: str | None) -> bool:
    return bool(s) and bool(re.match(r"^https?://\S+$", s.strip()))
```

**2.2 — Extract the dedup block into a helper** so lookup can turn domain-dedup OFF.
Current inline block is `_scout_news:1047–1061`. Replace with a call to:

```python
def _scout_dedup(urls: list[str], by_domain: bool, limit: int = 20) -> list[str]:
    """Dedup a URL list. by_domain=True keeps one per registrable host (discovery);
    by_domain=False dedups only exact-duplicate URLs (lookup keeps siblings)."""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if by_domain:
            try:
                host = urllib.parse.urlparse(u).hostname or ""
                key = host.removeprefix("www.")
            except Exception:
                key = u
        else:
            key = u
        if _scout_domain_excluded(key if by_domain else (urllib.parse.urlparse(u).hostname or "").removeprefix("www.")):
            continue
        if key and key not in seen:
            seen.add(key)
            out.append(u)
        if len(out) >= limit:
            break
    return out
```

**2.3 — Two lookup functions:**

```python
async def _scout_lookup_url(url: str) -> dict:
    """LOOKUP-URL: fetch exactly the pasted article. No search, no dedup, no date drop."""
    try:
        md = await _jina_read(url)
        res = _extract_news(md, url)
    except Exception as e:
        return {"results": [], "count": 0, "errors": [f"lookup {url}: {e}"], "query": url, "category": None, "days": 0}
    return {"results": [res] if res else [], "count": 1 if res else 0,
            "errors": [] if res else ["could not extract article (bot-check or empty page)"],
            "query": url, "category": None, "days": 0}


async def _scout_lookup_title(query: str, days: int) -> dict:
    """LOOKUP-TITLE: one tight search on the raw title. No scaffolding, no domain-dedup,
    no date drop. Reranked so the closest match floats up."""
    q = query.strip()
    ddg = []
    try:
        md = await _jina_read(f"https://duckduckgo.com/html/?q={urllib.parse.quote(q)}")
        ddg = [ev["url"] for ev in _parse_ddg(md)]
    except Exception:
        pass
    brave = await _brave_urls(q)
    gnews = await _gnews_urls(q)
    urls = _scout_dedup([*ddg, *brave, *gnews], by_domain=False, limit=20)

    async def _fx(u):
        try:
            return _extract_news(await _jina_read(u), u)
        except Exception:
            return None
    extracted = await asyncio.gather(*[_fx(u) for u in urls])
    ordered = [r for r in extracted if r]           # keep undated — no date drop on lookup
    ordered = await _scout_rerank(ordered)
    return {"results": ordered, "count": len(ordered), "errors": [],
            "query": query, "category": None, "days": days}
```

**2.4 — Branch in `_scout_news`** (top of the function, `:990`, after `days` clamp):

```python
async def _scout_news(query, category=None, days=7, exact=False):
    days = max(1, min(30, int(days or 7)))
    if _looks_like_url(query):
        return await _scout_lookup_url(query.strip())
    if exact and (query or "").strip():
        return await _scout_lookup_title(query.strip(), days)
    # ... existing discovery flow unchanged, but route its dedup block through
    #     _scout_dedup([*ddg_urls, *brave_urls, *gnews_urls], by_domain=True)
```

**2.5 — Thread `exact` through the endpoint** — `scout_search` (`:1291`) and
`_flow_scout_search` (`:1287`):

```python
# _flow_scout_search
async def _flow_scout_search(job, query, category, days, exact):
    job.result = await _scout_news(query=query, category=category, days=days, exact=exact)

# scout_search endpoint
exact = bool(payload.get("exact", False))
# ...
return _tn_spawn("scout-search", label,
                 lambda j: _flow_scout_search(j, query, category, days, exact))
```

### Frontend changes — `ThailandNowPanel.tsx`

**2.6 — `exact` toggle state** (with the other pitch state, ~L1501):

```ts
const [exact, setExact] = usePersistentState("tn.scout.exact", false);
```

**2.7 — Send it** — in `search()` (`:1520`):

```ts
const r = await post<{ id: string }>("/api/thailandnow/scout/search", { query, category, days, exact });
```

**2.8 — Checkbox in the pitch input row** (`:1619`, next to the SEARCH button `:1652`).
Also auto-detect URLs so the label teaches the behaviour:

```tsx
<label className="mono text-xs flex items-center gap-1" style={{ color: "var(--color-muted)" }}>
  <input type="checkbox" checked={exact} onChange={(e) => setExact(e.target.checked)} />
  Exact article
</label>
```

- Update the placeholder (`:1623`) to hint: `"topic, exact headline, or paste an article URL"`.
- When the input is a URL, the backend takes the LOOKUP-URL path automatically — the
  `exact` checkbox only matters for exact-title lookups.

**VERIFY:**
1. Paste a full article URL → SEARCH → exactly that one article comes back.
2. Tick **Exact article**, paste the exact headline text → that article appears in the
   results (not only its neighbours), even if a same-domain sibling exists.
3. Leave **Exact article** off, type a topic ("Thailand visa") → discovery behaves as
   before (broad, one-per-domain).

---

## Slice 3 — #4 SEND TO WP (upload selected images to the WP Media Library)

Selected IMAGE MODE images upload straight into WordPress Media, with metadata:
- **Alt Text** ← image description
- **Title** ← image name
- **Caption** ← `Source: <domain without http/www> / Website`

Fields are **auto-filled AND editable** before upload (Naz's call — this writes to the
live media library, so no silent uploads).

### Backend — `app/thailandnow.py`

Reuse the existing WP client: `_wp_creds()` (`:2112`, Basic-auth app-password) and `_wp()`
(`:2133`). Media upload needs a **binary** POST, which `_wp()` can't do (it only sends
JSON) — add a dedicated function near `_wp()`:

```python
_WP_MEDIA_MAX_BYTES = 15 * 1024 * 1024  # 15 MB guard
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

async def _wp_upload_media(image_url: str, title: str, alt_text: str, caption: str) -> dict:
    """Fetch an image by URL and upload it to WP /media, then set title/alt/caption.
    Returns {id, source_url, link}. Raises HTTPException on any failure."""
    url, user, pwd = _wp_creds()
    # 1. fetch bytes (browser UA — many news CDNs 403 a bare client)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        img = await c.get(image_url, headers={"User-Agent": _BROWSER_UA})
    if img.status_code >= 400:
        raise HTTPException(502, f"fetch image {img.status_code} — source may block hotlinking")
    ctype = img.headers.get("content-type", "").split(";")[0].strip()
    if not ctype.startswith("image/"):
        raise HTTPException(415, f"not an image (content-type {ctype or 'unknown'})")
    if len(img.content) > _WP_MEDIA_MAX_BYTES:
        raise HTTPException(413, "image exceeds 15 MB")
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype, "jpg")
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "image").lower()).strip("-")[:60] or "image"
    filename = f"{slug}.{ext}"
    # 2. upload binary
    async with httpx.AsyncClient(timeout=60, auth=(user, pwd)) as c:
        up = await c.post(
            f"{url}/wp-json/wp/v2/media",
            content=img.content,
            headers={"Content-Disposition": f'attachment; filename="{filename}"', "Content-Type": ctype},
        )
    if up.status_code >= 400:
        raise HTTPException(502, f"WP media upload {up.status_code}: {up.text[:200]}")
    media = up.json()
    mid = media.get("id")
    # 3. set metadata explicitly (title/caption on upload are unreliable; alt_text is upload-ignored)
    updated = await _wp("POST", f"/media/{mid}", json_body={
        "title": title or filename,
        "alt_text": alt_text or "",
        "caption": caption or "",
    })
    return {"id": mid, "source_url": (updated or media).get("source_url"), "link": (updated or media).get("link")}
```

**Endpoint** (near the other scout routes, ~L1344):

```python
@router.post("/api/thailandnow/scout/wp-media")
async def scout_wp_media(payload: dict = Body(default={})):
    """SEND TO WP — upload one image to the Media Library with metadata.
    Fields arrive already-final (frontend is editable), so we upload them verbatim."""
    image_url = (payload.get("image_url") or "").strip()
    if not image_url:
        raise HTTPException(400, "image_url required")
    return await _wp_upload_media(
        image_url=image_url,
        title=(payload.get("title") or "").strip(),
        alt_text=(payload.get("alt_text") or "").strip(),
        caption=(payload.get("caption") or "").strip(),
    )
```

> Caption is sent **pre-formatted** from the frontend (WYSIWYG, since it's editable). The
> `Source: <domain> / Website` string is built client-side (next section).

### Frontend — `ThailandNowPanel.tsx` (IMAGE MODE panel, `:1750`)

Add a **selection + review-and-send** flow across Tier 1 (article) and Tier 2 (stock).
Tier 3 (AI prompts) has no images → not selectable.

**3.1 — New state** in `StoryScoutTab`:

```ts
type WpDraft = { image_url: string; title: string; alt_text: string; caption: string };
const [selected, setSelected] = useState<Record<string, WpDraft>>({});   // key = image_url
const [wpSending, setWpSending] = useState(false);
const [wpStatus, setWpStatus] = useState<Record<string, string>>({});    // image_url -> "✓ #123" | "err: ..."
```

**3.2 — Caption/title/alt default helpers** (module-scope, near the top of the file):

```ts
function bareDomain(u: string): string {
  try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; }
}
// tier1: article image — source is the ARTICLE domain (scoutImgData.url)
// tier2: stock — source is the provider domain
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
```

**3.3 — Selection UI:** add a checkbox to each Tier 1 card (`:1805`) and Tier 2 card
(`:1826`). Toggling adds/removes a `WpDraft` (seeded from `wpDefaults`) in `selected`.

**3.4 — Review-and-send bar** (sticky, shown when `Object.keys(selected).length > 0`),
below the tiers. For each selected image render three editable inputs (Title, Alt text,
Caption) pre-filled from the draft, each `onChange` patching `selected[url]`. One button:

```tsx
<button className="btn btn--signal" disabled={wpSending} onClick={sendSelectedToWp}>
  {wpSending ? "SENDING…" : `SEND ${Object.keys(selected).length} TO WP`}
</button>
```

**3.5 — Sequential upload** (one at a time so a live media library isn't hammered, and
each row gets its own status):

```ts
const sendSelectedToWp = useCallback(async () => {
  setWpSending(true);
  for (const [url, draft] of Object.entries(selected)) {
    setWpStatus((s) => ({ ...s, [url]: "sending…" }));
    const r = await post<{ id: number; link: string }>("/api/thailandnow/scout/wp-media", draft);
    setWpStatus((s) => ({ ...s, [url]: r.ok && r.data ? `✓ #${r.data.id}` : `err: ${r.error}` }));
  }
  setWpSending(false);
}, [selected]);
```

Show `wpStatus[url]` next to each row. On `✓`, optionally auto-deselect that image.

**VERIFY (do this on a test image first, not a hero image):**
1. IMAGE MODE → FIND IMAGES on any article → select one Tier 1 and one Tier 2 image.
2. The review bar shows both with prefilled Title / Alt / Caption. Caption reads
   `Source: <domain> / Website` — Tier 1 = the article's domain, Tier 2 = pexels/pixabay.
3. Edit a title, click SEND → each row shows `✓ #<id>`.
4. In WP Admin → Media, open the new items: **Title**, **Alt Text**, and **Caption**
   match what was sent (caption in the exact `Source: … / Website` format).
5. A hotlink-blocked source returns `err: … blocks hotlinking` on that row only — the
   batch keeps going. No crash.

---

## Slice 4 — TERMINAL DISCOVERY (build). Un-cut 2026-07-27 per Naz.

**Decision (Naz, 2026-07-27):** the pipeline discovery is mediocre at the *judgment* task
of "find me pitchable stories." A real Claude agent (`/f5-story-scout`) searches, reads,
reformulates and judges — it out-pitches the fixed sweep, and it runs on the **subscription
session, not metered API**. So we add an agent-driven discovery path. Slice 2's paste-URL /
exact-headline lookup **stays** as the instant quick path (Naz's call); this is additive.

**Three locked design choices (Naz picked all three "recommended"):**
1. **File handoff, not screen-scrape.** The skill writes JSON to a fixed path; CONVERT reads
   it. Robust, exact contract, survives terminal redraws. **No `capture-pane` endpoint.**
2. **Type-only + manual Enter.** SEARCH *types* `/f5-story-scout …` into the shared tmux
   session via the existing `POST /api/terminal/insert`; Naz presses Enter himself. The
   type-only safety gate (`app/terminal_input.py`) is **kept — do not automate Enter.**
3. **No new embedded terminal.** Reuse the existing **LIVE dock** (`config.dock`, ttyd at
   `http://localhost:7681`, tmux session `main`). The insert bridge injects into session
   `main`, which **mirrors to every attached client** (TERMINAL tab + LIVE dock). A *third*
   iframe would shrink the tmux window to the smallest client and degrade the main terminal
   (tmux sizes to smallest attached client) — **so we do NOT embed one.**

> **Operational prerequisite (tell Naz, not a code item):** a `claude` session must be
> running in tmux session `main` (open the LIVE dock / TERMINAL and run `claude`). The
> `f5-story-scout` skill is a **global** user skill, so it loads in any dir — no need to be
> in the Railjack folder. If `main` is a plain shell, the injected `/f5-story-scout` line
> does nothing.

### 4a — Skill JSON handoff — `/home/NAZ/.claude/skills/f5-story-scout/SKILL.md` (OUTSIDE the repo)

Today PITCH mode outputs a **markdown table only** (`Link | Lang | Date | pitch angle | Flags`)
— no machine-readable output. Add a new numbered step to PITCH mode, right after the output
table step, so it *also* writes JSON. **Field names must match `ScoutResult` exactly**
(`title, url, snippet, date, lang, source`) so CONVERT is a near-zero mapping.

Paste this as a new step under PITCH mode:

```
N. **Also emit machine-readable JSON (in ADDITION to the table — never instead).**
   After the tables, `mkdir -p /tmp/railjack-scout` then write the SAME stories as a
   single JSON array to `/tmp/railjack-scout/latest.json` (overwrite). The array is the
   flat union of every table row this run. Each element has EXACTLY these fields:
     - "title":   article headline (original language)
     - "url":     full canonical article URL, bare (not a markdown link)
     - "snippet": 15-20 word plain-text pitch angle / why it's pitchable (no markdown/newlines)
     - "date":    publish date ISO 8601 (YYYY-MM-DD), or "" if unknown
     - "lang":    "th" or "en"
     - "source":  publisher bare domain, lowercased, no scheme, no leading "www."
   Write valid UTF-8 (do NOT \uXXXX-escape Thai). Emit [] if no stories. Keep this path
   and schema FIXED even if the table format changes — it's an integration handoff.
```

Query passes with **no skill change**: typing `/f5-story-scout pitch "<query>"` hands the
topic to the skill as the request text (it already reads the topic from "the one ask").

> This file is outside the Railjack repo. If the IDE won't touch files outside its workspace,
> **Tawhan applies this one edit**; the backend/frontend below are the IDE's.

### 4b — Backend CONVERT endpoint — `app/thailandnow.py`

No terminal-output capture. One read-only endpoint that parses the handoff file. Add near
the other scout routes:

```python
_SCOUT_HANDOFF = Path("/tmp/railjack-scout/latest.json")

@router.get("/api/thailandnow/scout/terminal-report")
async def scout_terminal_report():
    """CONVERT — read the JSON the /f5-story-scout skill wrote to disk. Returns
    {results, count, mtime}. 404 until the skill has written a file."""
    p = _SCOUT_HANDOFF
    if not p.exists():
        raise HTTPException(404, "no scout handoff yet — run SCOUT, let Claude finish (writes /tmp/railjack-scout/latest.json)")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(422, f"handoff isn't valid JSON: {e}")
    if not isinstance(raw, list):
        raise HTTPException(422, "handoff must be a JSON array")
    results = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        u = str(it.get("url") or "").strip()
        if not u:
            continue  # url is the React key + pitch key — must be present & unique
        results.append({
            "title":   str(it.get("title") or u).strip(),
            "url":     u,
            "snippet": str(it.get("snippet") or it.get("excerpt") or "").strip(),
            "date":    str(it.get("date") or "").strip(),
            "lang":    str(it.get("lang") or "").strip(),
            "source":  str(it.get("source") or "").strip(),
        })
    # dedup on url, preserve order (dup urls would collide as React keys)
    seen, deduped = set(), []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"]); deduped.append(r)
    return {"results": deduped, "count": len(deduped), "mtime": p.stat().st_mtime}
```

- `json` and `Path` are already imported in this module (used by the gem loaders). Confirm;
  don't double-import.
- Accepts `excerpt` as a fallback for `snippet` so a slightly-off skill run still renders.

### 4c — Frontend SCOUT + CONVERT — `ThailandNowPanel.tsx` (`StoryScoutTab`, PITCH mode)

Two `btn--compact` buttons beside the existing SEARCH button in the pitch input row, plus a
one-line hint. **No mode toggle, no iframe** — the terminal is the LIVE dock.

**State** (with the other pitch state):
```ts
const [lastScoutAt, setLastScoutAt] = useState<number>(0);
```

**SCOUT — inject the command** (types into tmux `main`; Naz presses Enter in the dock):
```ts
const scoutViaClaude = useCallback(async () => {
  const q = query.trim();
  if (!q) { setErr("type a topic first"); return; }
  const cmd = `/f5-story-scout pitch "${q.replace(/["\n\r]/g, "'")}"`;   // no quotes/newlines: insert is type-only, <500 chars
  const r = await post<{ status: string }>("/api/terminal/insert", { text: cmd });
  if (!r.ok) { setErr(r.error || "couldn't reach the terminal — is ttyd/tmux up?"); return; }
  setLastScoutAt(Date.now());
  setErr("Typed into the LIVE terminal. Open the LIVE dock, press Enter to run, wait for it to finish, then click CONVERT.");
}, [query]);
```

**CONVERT — read the handoff → cards** (bypasses the job pipeline; `setResults` directly):
```ts
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
```

**Buttons** (after the SEARCH button at the end of the pitch input row):
```tsx
<button className="btn btn--compact" onClick={scoutViaClaude}>SCOUT ▸ CLAUDE</button>
<button className="btn btn--compact" onClick={convertFromClaude}>CONVERT ◂ JSON</button>
```

**Hint line** under the input row:
```tsx
<div className="mono text-xs" style={{ color: "var(--color-muted)" }}>
  SCOUT types the command into the LIVE terminal — open the LIVE dock, press Enter there,
  wait for Claude to finish, then CONVERT. (SEARCH is still the instant URL / exact-headline path.)
</div>
```

Converted results flow through the **existing** card render, so FIND IMAGES and MAKE PITCH
work unchanged — each item's `url` is the key, so the skill must emit unique URLs.

### VERIFY (Slice 4)
1. **Prereq:** a `claude` session is running in tmux `main` (LIVE dock shows a Claude prompt).
2. PITCH mode → type "Thailand visa" → click **SCOUT ▸ CLAUDE**. The LIVE dock shows
   `/f5-story-scout pitch "Thailand visa"` at the prompt (not executed).
3. Press Enter in the dock. Claude runs, prints the table, and writes
   `/tmp/railjack-scout/latest.json` (check: file exists, valid JSON array).
4. Click **CONVERT ◂ JSON** → the result cards populate from the file. Each card shows
   title/source/date/lang, and **FIND IMAGES** + **MAKE PITCH** work on them.
5. Click CONVERT with no file / before the run finishes → clean error message, no crash.
6. Stale check: CONVERT when the file predates your last SCOUT → the "older than your last
   SCOUT" warning shows but still renders what's there.

---

## ACCEPTANCE BAR for Slice 2 — pass or the pitch feature is SCRAPPED

Naz's condition: refine the pitch search once. If it doesn't clear this bar, **rip the
pitch feature out** (remove PITCH MODE + its backend routes) rather than iterate further
or fall back to a terminal. Test with real inputs, Tawhan verifies:

- **T1 (URL lookup):** paste a real article URL → the result is **that exact article**,
  first and correct. Not a neighbour, not empty.
- **T2 (exact title):** tick *Exact article*, paste the exact headline → **that article
  appears in the results**, even when a same-domain sibling exists. This is the case that
  failed before; it is the make-or-break test.
- **T3 (no regression):** *Exact article* off, type a topic → discovery still returns a
  broad, one-per-domain spread as it does today.

**Pass** = T1 and T2 both work on ≥3 real examples Naz supplies. **Fail** = the pitch
feature is removed; only IMAGE MODE (Slices 1 + 3) survives on the STORY SCOUT tab.

---

## Build order & landing bar

1. ~~**Slice 0** — verify #3, no code.~~ ✅ done (was already shipped).
2. ~~**Slice 1** — persist image results.~~ ✅ `ef36df0`.
3. ~~**Slice 2** — LOOKUP pipeline fix.~~ ✅ `cf904eb`, passed acceptance bar (T1/T2/T3).
4. ~~**Slice 3** — SEND TO WP.~~ ✅ `a21d1db`, live-verified round-trip.
5. **Slice 4** — TERMINAL DISCOVERY (NEXT). Order: **4a skill → 4b backend → 4c frontend.**
   4a (global skill file) may be Tawhan's to apply if the IDE is workspace-scoped. Slice 2's
   lookup stays as the quick path — do not remove it.

**Landing bar per slice:** `uv run pytest` green · `ruff` clean · the slice's VERIFY block
passes in the live app at `:8700`. **Restart uvicorn after every slice** — the deployment has
no `--reload`, so a stale process silently serves old code (this bit us twice; verify against
a *restarted* server). Rebuild frontend: `cd frontend && npm run build`, then hard-reload.
`tests/test_scout.py` exists (4 tests). **Add for Slice 4b:** a test that writes a temp
`latest.json` (monkeypatch `_SCOUT_HANDOFF` to a tmp_path), asserts the endpoint parses,
maps `excerpt`→`snippet`, drops url-less rows, and dedups. Tawhan verifies before commit.

**Slice 4 acceptance:** softer than Slice 2 (this is additive, not on probation). Pass =
the VERIFY block runs end-to-end on ≥1 real query, cards render, FIND IMAGES + MAKE PITCH
work on converted results. If the agent can't reliably write clean JSON, the fallback is to
lean on Slice 2's lookup — the pitch feature is NOT scrapped (that clause was Slice 2 only).
