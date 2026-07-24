"""Thailand NOW — monthly content-pipeline module (WRITERS + EVENTS).

Two halves share one desk-driven create/attach engine:
  WRITERS — bulk-create blank Google Docs + Trello cards per writer (Paul/Teerin).
  EVENTS  — scout upcoming Thailand events, generate a publicity bundle, then
            spin up a prefilled Doc + card (TIAN desk).

Desks (Paul/Teerin/TIAN) are config rows in configs/tawhan.yaml → options.desks,
so adding/changing a writer is a YAML edit, not code. REST-only via httpx — no
google-api-python-client, no py-trello. LLM calls route through app/zai.py (the
one place we call z.ai). See docs/thailandnow-plan.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Body, HTTPException

from .config import CONFIG
from .zai import zai_message

router = APIRouter()


def _opts() -> dict:
    """This module's options: block, read fresh each call.

    reload_config() mutates CONFIG in place (app/config.py:141), so the CONFIG
    reference stays valid after ↻ CFG — but a *cached* snapshot of options would
    go stale (the old .options dict survives while CONFIG.modules is replaced).
    Read per-request so a desk-count edit + reload is immediately live.
    """
    for m in CONFIG.modules:
        if m.kind == "panel" and m.panel == "thailandnow":
            return m.options or {}
    return {}


# --- WRITERS / shared engine (slices 1-4) ---


@router.get("/api/thailandnow/desks")
def get_desks() -> dict:
    """Desk config for the frontend. WRITERS renders the list; the TIAN create is
    driven server-side. Drive folder ids are not secrets (need auth to act on)."""
    opts = _opts()
    return {
        "desks": opts.get("desks", []),
        "trello_board_short": opts.get("trello_board_short", ""),
        "ready": bool(opts.get("desks")),
    }


# --- name-token resolution + {nn} dedup (pure; self-checked) ---


def _yyyymm_mon(when: datetime | None = None) -> tuple[str, str]:
    d = when or datetime.now()
    return d.strftime("%Y%m"), d.strftime("%b").upper()  # "202607", "JUL"


def _mon_for(yyyymm: str) -> str | None:
    """3-letter month for a YYYYMM override, or None if it isn't a real month. Lets Naz pin
    the doc month instead of always using the current one (R1)."""
    try:
        return datetime.strptime(yyyymm, "%Y%m").strftime("%b").upper()
    except (ValueError, TypeError):
        return None


def _resolve_name(template: str, yyyymm: str, mon: str, nn: int, title: str | None) -> str:
    """Substitute every token in a doc/card name template. {nn} is zero-padded to
    2 (monthly counts stay <100). [CAT] is left literal — Ben fills it."""
    return (
        template.replace("{yyyymm}", yyyymm)
        .replace("{mon}", mon)
        .replace("{nn}", f"{nn:02d}")
        .replace("{title}", title or "")
    )


def _nn_regex(template: str, yyyymm: str, mon: str, title: str | None) -> re.Pattern:
    """Regex matching names equal to ``template`` with {nn} = some digits."""
    s = template.replace("{yyyymm}", yyyymm).replace("{mon}", mon).replace("{title}", title or "")
    parts = s.split("{nn}")
    if len(parts) != 2:
        return re.compile(r"\A\Z")  # no {nn} → matches nothing
    return re.compile(re.escape(parts[0]) + r"(\d+)" + re.escape(parts[1]) + r"\Z")


def _next_nn(template: str, yyyymm: str, mon: str, title: str | None, names: list[str]) -> int:
    """Max existing #NN for this template+month, +1 (min 1). Re-running mid-month
    never re-creates #01 — it continues from the highest seen."""
    rx = _nn_regex(template, yyyymm, mon, title)
    mx = 0
    for n in names:
        m = rx.match(n)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _date_rule(start: str | None, end: str | None, signup: str | None) -> tuple[str | None, str | None]:
    """Trello card (start, due) ISO datetimes from raw event dates (R3). Pure + self-checked.

    due  = end_date, else start_date.
    start = due − 7 days when a signup deadline exists (Naz: start prep a week out);
            elif the event spans >1 day → the event start;
            else None (single-day, no signup → due only).
    Returns (None, None) when no date is parseable at all.
    """

    def _iso(d: str | None) -> str | None:
        if not d:
            return None
        try:
            return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00.000Z")
        except ValueError:
            return None

    due_iso = _iso(end) or _iso(start)
    if not due_iso:
        return (None, None)
    due_day = datetime.strptime(due_iso[:10], "%Y-%m-%d")
    if signup:
        start_iso = (due_day - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00.000Z")
    elif end and start and end[:10] != start[:10]:
        start_iso = _iso(start)
    else:
        start_iso = None
    return (start_iso, due_iso)


# --- Google (documents + drive scopes) ---


def _google_token_path() -> Path:
    return Path(os.path.expanduser(_opts().get("google_token_path", "~/.config/railjack/google_token.json")))


async def _google_token() -> str:
    """Load + refresh the Google OAuth token. Minted once via ``python3 -m
    app.tn_auth`` → options.google_token_path. HTTPException(503) until minted."""
    path = _google_token_path()
    if not path.exists():
        raise HTTPException(
            503, "Google token not minted — run `python3 -m app.tn_auth` once "
            "(needs the OAuth client_id/secret for documents+drive scopes)",
        )
    d = json.loads(path.read_text())
    data = urllib.parse.urlencode({
        "client_id": d["client_id"], "client_secret": d["client_secret"],
        "refresh_token": d["refresh_token"], "grant_type": "refresh_token",
    })
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(d["token_uri"], content=data,
                         headers={"content-type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise HTTPException(502, f"Google token refresh failed: {r.text[:200]}")
        return r.json()["access_token"]


async def _google_create_doc(token: str, folder_id: str, name: str, body: str) -> str:
    """Create a Doc in ``folder_id``, make it link-shareable, optionally write
    ``body`` (Articles/Blogs pass none → blank). Returns the Doc's webViewLink."""
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://www.googleapis.com/drive/v3/files", headers=hdr,
            json={"name": name, "parents": [folder_id],
                  "mimeType": "application/vnd.google-apps.document"},
        )
        r.raise_for_status()
        doc_id = r.json()["id"]
        await c.post(
            f"https://www.googleapis.com/drive/v3/files/{doc_id}/permissions",
            headers=hdr, params={"sendNotificationEmail": "false"},
            json={"role": "reader", "type": "anyone"},
        )
        if body:
            await c.post(
                f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
                headers=hdr,
                json={"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]},
            )
        meta = await c.get(
            f"https://www.googleapis.com/drive/v3/files/{doc_id}",
            headers=hdr, params={"fields": "webViewLink"},
        )
        return meta.json().get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"


# --- Trello (key+token as query params) ---


def _trello_creds() -> tuple[str, str]:
    key = os.environ.get("TRELLO_KEY", "")
    tok = os.environ.get("TRELLO_TOKEN", "")
    if not key or not tok:
        raise HTTPException(503, "TRELLO_KEY/TRELLO_TOKEN not in the service env")
    return key, tok


async def _trello(method: str, path: str, params: dict | None = None, body: dict | None = None):
    key, tok = _trello_creds()
    q = {"key": key, "token": tok, **(params or {})}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"https://api.trello.com/1{path}", params=q, json=body)
        if r.status_code >= 400:
            raise HTTPException(502, f"Trello {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}


async def _trello_list_id(board_short: str, list_name: str) -> str:
    """Resolve a list name → id. Case-insensitive + trimmed: the board's list
    names are ALL-CAPS ('To draft (PAUL)') while the config is mixed-case."""
    lists = await _trello("GET", f"/boards/{board_short}/lists", {"fields": "name,id"})
    want = list_name.strip().lower()
    for l in lists:
        if l["name"].strip().lower() == want:
            return l["id"]
    raise HTTPException(
        400, f"no Trello list matching {list_name!r}; have {[l['name'] for l in lists]}"
    )


async def _trello_label_ids(board_short: str, names: list[str]) -> list[str]:
    """Resolve label names → ids, case-insensitively (R3). The board already holds the target
    labels (Quota / Happening NOW / Events NOW); a typo raises naming what's available rather
    than silently creating a stray label."""
    if not names:
        return []
    labels = await _trello("GET", f"/boards/{board_short}/labels", {"fields": "name,id"})
    by = {l["name"].strip().lower(): l["id"] for l in labels}
    ids: list[str] = []
    missing: list[str] = []
    for n in names:
        nid = by.get(n.strip().lower())
        if nid:
            ids.append(nid)
        else:
            missing.append(n)
    if missing:
        raise HTTPException(
            400, f"labels not on board {board_short!r}: {missing}; "
            f"have {[l['name'] for l in labels]}"
        )
    return ids


@router.post("/api/thailandnow/provision")
async def provision(payload: dict = Body(default={})):
    """The shared create/attach engine. For each of N items on a desk: resolve the
    next #NN (scanning existing cards in the desk's list), create a Google Doc in
    the desk's Drive folder (blank unless ``body`` is given), make it
    link-shareable, create a Trello card in the desk's list, and attach the Doc to
    the card. Returns each {doc,card} pair.

    The Google step 503s until the token is minted (``python3 -m app.tn_auth``);
    Trello + dedup resolution happen first, so the gate message reports the next
    #NN it *would* create."""
    body = payload or {}
    desk_id = body.get("desk_id")
    opts = _opts()
    desk = next((d for d in opts.get("desks", []) if d["id"] == desk_id), None)
    if not desk:
        raise HTTPException(400, f"unknown desk_id {desk_id!r}")
    board = opts.get("trello_board_short", "")
    count = int(body.get("count") or desk.get("count") or 1)
    # R1: Naz can pin the doc month (defaults to current). {nn} dedup keys off this, so an
    # overridden month continues that month's sequence instead of the live one.
    yyyymm_in = (body.get("yyyymm") or "").strip()
    if yyyymm_in:
        mon = _mon_for(yyyymm_in)
        if not mon:
            raise HTTPException(400, f"bad yyyymm {yyyymm_in!r}; use YYYYMM e.g. 202608")
        yyyymm = yyyymm_in
    else:
        yyyymm, mon = _yyyymm_mon()
    title = body.get("title")
    doc_body = body.get("body") or ""
    card_desc = body.get("card_desc") or ""
    # R2: callers (the EVENTS flow) may override the name templates so a titled doc can coexist
    # with the desk's blank-friendly default.
    doc_name_tpl = body.get("doc_name") or desk["doc_name"]
    card_name_tpl = body.get("card_name") or desk["card_name"]
    # R3: optional card dates (ISO) + per-desk labels (resolved by name).
    due = body.get("due")
    start = body.get("start")

    list_id = await _trello_list_id(board, desk["trello_list_name"])
    label_ids = await _trello_label_ids(board, desk.get("labels", []))
    cards = await _trello("GET", f"/lists/{list_id}/cards", {"fields": "name"})
    nn = _next_nn(card_name_tpl, yyyymm, mon, title, [c["name"] for c in cards])

    if not _google_token_path().exists():
        raise HTTPException(
            503, f"resolved next #{nn:02d} for {desk_id} ({count}×, list "
            f"{desk['trello_list_name']!r}), but the Google token isn't minted — "
            "run `python3 -m app.tn_auth`, then retry.",
        )
    token = await _google_token()

    out = []
    for i in range(count):
        cur = nn + i
        doc_name = _resolve_name(doc_name_tpl, yyyymm, mon, cur, title)
        card_name = _resolve_name(card_name_tpl, yyyymm, mon, cur, title)
        doc_url = await _google_create_doc(token, desk["drive_folder_id"], doc_name, doc_body)
        card_params = {"idList": list_id, "name": card_name, "desc": card_desc,
                       "idLabels": ",".join(label_ids)}
        if due:
            card_params["due"] = due
        if start:
            card_params["start"] = start
        card = await _trello("POST", "/cards", card_params)
        await _trello("POST", f"/cards/{card['id']}/attachments",
                      body={"url": doc_url, "name": doc_name})
        out.append({"nn": cur, "doc_name": doc_name, "doc_url": doc_url,
                    "card_name": card_name, "card_url": card.get("url", "")})
    return {"desk_id": desk_id, "count": count, "yyyymm": yyyymm, "items": out}


# --- EVENTS radar (slices 5-8) — keyless Jina Reader first ---


async def _jina_read(url: str, timeout: float = 30.0) -> str:
    """Fetch a URL as clean markdown via Jina Reader (keyless: r.jina.ai/<url>).
    Used by the events scout, the publicity fetch, and the image scrape."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(f"https://r.jina.ai/{url}")
        r.raise_for_status()
        return r.text


# --- LLM → structured JSON (R5): zai returns text; parse it defensively ---


def _parse_json_lenient(text: str):
    """Parse JSON from an LLM string, tolerating ```json fences and surrounding prose.
    Returns the parsed object (usually a list) or None."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).strip()
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = s.find(opener), s.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


async def _llm_json(prompt: str, system: str | None = None, model: str | None = None):
    """Ask the LLM for JSON and parse it (lenient). None on any failure."""
    raw = await zai_message(prompt, max_tokens=4096, system=system, model=model, timeout=120)
    return _parse_json_lenient(raw)


def _iso_date(s) -> str | None:
    """Coerce a date-ish string to YYYY-MM-DD, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _normalize_event(ev, today_iso: str, window_end_iso: str, source: str = "scout"):
    """Validate + clip one LLM-extracted event. Returns None if it has no usable start
    date or falls outside the [today, window_end] window — the hard future filter (R5)."""
    if not isinstance(ev, dict):
        return None
    start = _iso_date(ev.get("start_date"))
    if not start or start < today_iso or start > window_end_iso:
        return None
    return {
        "title": (ev.get("title") or "").strip()[:200] or "Untitled",
        "url": (ev.get("url") or "").strip(),
        "start_date": start,
        "end_date": _iso_date(ev.get("end_date")) or "",
        "signup_deadline": _iso_date(ev.get("signup_deadline")) or "",
        "location": (ev.get("location") or "").strip()[:120],
        "language": (ev.get("language") or "en").strip()[:4] or "en",
        "summary": (ev.get("summary") or "").strip()[:300],
        "source": source,
    }


# DuckDuckGo HTML hides the real URL in the urlencoded uddg= query param.
_DDG_RE = re.compile(r"##\s*\[([^\]]+)\]\(https://duckduckgo\.com/l/\?uddg=([^)&]+)")


def _parse_ddg(md: str) -> list[dict]:
    out: list[dict] = []
    for title, uddg in _DDG_RE.findall(md):
        url = urllib.parse.unquote(uddg)
        title = title.strip()
        if title and url.startswith("http"):
            out.append(
                {"title": title, "url": url, "date": "", "location": "", "source": "duckduckgo"}
            )
    return out


# Default scout sources (R5). Override per-machine via options.event_listings /
# options.event_queries in configs/<machine>.yaml. Keyless — TAT + aggregators + broad DDG.
_DEFAULT_LISTINGS = [
    "https://www.tourismthailand.vn/Events",
    "https://www.tourismthailand.org/Events",
    "https://allconferencealert.net/country/thailand/",
    "https://10times.com/thailand",
]
_EXTRACT_SYS = (
    "You extract structured event data from web pages as JSON. Return ONLY a JSON array — "
    "no prose, no code fence. Each element has keys title, url, start_date, end_date, "
    "signup_deadline (all dates YYYY-MM-DD or null), location, language (en/th), summary. "
    "Skip anything without a real start date. Dates are absolute, never relative."
)


async def _extract_events_from(url: str, today_iso: str, window_end_iso: str, category: str,
                               source: str) -> tuple[list[dict], str | None]:
    """Jina-fetch one page, ask the LLM for a JSON array of dated events, then normalize +
    window-filter each. Returns (events, error_or_none)."""
    try:
        md = await _jina_read(url, timeout=30.0)
    except Exception as e:
        return [], f"fetch {url}: {e}"
    cat = f" Focus area: {category}." if category else ""
    user = (
        f"Today is {today_iso}. List upcoming events on this page whose start date is within "
        f"{today_iso} to {window_end_iso}.{cat}\n"
        f"Page ({url}):\n{md[:9000]}"
    )
    arr = await _llm_json(user, system=_EXTRACT_SYS)
    if not isinstance(arr, list):
        return [], None
    out = [_normalize_event(ev, today_iso, window_end_iso, source) for ev in arr]
    return [e for e in out if e], None


@router.post("/api/thailandnow/events/scout")
async def scout_events(payload: dict = Body(default={})):
    """Events radar (R5) — future Thailand events only, multi-source, dated + filtered.

    Tier 1 / keyless: direct event-listing pages (TAT, allconferencealert, 10times) +
    DuckDuckGo broad queries (catches news mentions), each fetched via Jina with the LLM
    extracting a dated JSON array per page. Results are window-filtered (start_date within
    today → today+weeks), deduped, and sorted by start_date. ``weeks`` is 1..52 (default 4).
    No API keys required. (Tier 2 / NotebookLM deep research lives at /events/deep.)
    """
    body = payload or {}
    category = (body.get("query") or "").strip()
    weeks = max(1, min(52, int(body.get("weeks") or 4)))
    opts = _opts()
    today = datetime.now()
    window_end = today + timedelta(weeks=weeks)
    today_iso, window_end_iso = today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
    span = today.strftime("%B %Y")

    # candidate URLs: direct listings + DuckDuckGo broad results
    candidates: dict[str, None] = {}
    for u in (opts.get("event_listings") or _DEFAULT_LISTINGS):
        candidates.setdefault(u, None)
    errors: list[str] = []
    cat_q = f" {category}" if category else ""
    queries = opts.get("event_queries") or [
        f"Thailand events{cat_q} {span} conference festival exhibition",
        f"upcoming Thailand events{cat_q} {span} seminar expo Bangkok",
    ]
    for q in queries:
        try:
            md = await _jina_read(f"https://duckduckgo.com/html/?q={urllib.parse.quote(q)}")
            for ev in _parse_ddg(md):
                candidates.setdefault(ev["url"], None)
        except Exception as e:
            errors.append(f"ddg {q!r}: {e}")

    urls = list(candidates)[:8]
    results = await asyncio.gather(*[
        _extract_events_from(u, today_iso, window_end_iso, category, "scout") for u in urls
    ])
    events: dict[str, dict] = {}
    for evs, err in results:
        if err:
            errors.append(err)
            continue
        for ev in evs:
            events.setdefault(ev["url"] or ev["title"], ev)
    ordered = sorted(events.values(), key=lambda e: e.get("start_date") or "9999")
    return {"events": ordered, "count": len(ordered), "errors": errors,
            "window": {"from": today_iso, "to": window_end_iso, "weeks": weeks}}


def _gem_path() -> Path:
    """Resolve the publicity gem path (relative paths anchor at the repo root)."""
    p = Path(_opts().get("gem_path", "app/gems/event-publicity.md"))
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def _load_gem(path: Path) -> str:
    """Read the publicity gem and extract just the prompt body. The module-local
    file mirrors the vault canonical (frontmatter + intro + notes); the prompt
    proper runs from '## Role & Purpose' to the trailing '---' separator."""
    text = path.read_text(encoding="utf-8")
    start = text.find("## Role & Purpose")
    body = text[start:] if start != -1 else text
    cut = body.find("\n---\n")
    if cut != -1:
        body = body[:cut]
    return body.strip()


@router.post("/api/thailandnow/events/publicize")
async def publicize_event(payload: dict = Body(default={})):
    """Generate the 5-part publicity bundle for an event. Jina-fetches each URL,
    concatenates as raw info, then calls the publicity LLM with the gem prompt as
    the system role. Returns the plain-text bundle (FB / X / IG / Meta / Long-form)
    — shown editable in the UI before any doc is created (nothing auto-committed)."""
    body = payload or {}
    event = body.get("event") or {}
    urls = body.get("urls") or ([event["url"]] if event.get("url") else [])
    if not urls:
        raise HTTPException(400, "no event urls provided")
    chunks: list[str] = []
    for u in urls:
        try:
            chunks.append(await _jina_read(u))
        except Exception as e:
            chunks.append(f"[fetch failed for {u}: {e}]")
    raw = "\n\n---\n\n".join(chunks)
    system = _load_gem(_gem_path())
    model = (_opts().get("publicity_llm") or {}).get("model") or "glm-5"
    user = (
        f"Event title: {event.get('title', '(unknown)')}\n"
        f"Source URL(s): {', '.join(urls)}\n\n"
        f"Raw event information (Thai or English, any form):\n{raw[:20000]}"
    )
    bundle = await zai_message(
        user, max_tokens=8192, system=system, model=model, timeout=180
    )
    return {"bundle": bundle, "event": event, "urls": urls, "model": model}


# --- image lookup (slice 7) — event-page scrape first; stock fallback deferred ---

_IMG_RE = re.compile(r"!\[([^\]]*)\]\((https://[^)\s]+\.(?:jpe?g|png|webp|avif))\)", re.I)
_IMG_SKIP = re.compile(
    r"icon|logo|hamburger|spinner|placeholder|blank|sprite|blur|thumb|avatar|favicon", re.I
)


def _parse_images(md: str, limit: int = 12) -> list[dict]:
    """Extract content images from an event page's Jina markdown. Skip nav/
    branding art and rank larger crops higher (WP/etc. URLs carry a ``-WxH`` hint
    like ``-1024x682``)."""
    seen: dict[str, dict] = {}
    for alt, url in _IMG_RE.findall(md):
        if _IMG_SKIP.search(url) or _IMG_SKIP.search(alt):
            continue
        url = url.split("?")[0]
        if url in seen:
            continue
        m = re.search(r"-(\d+)x(\d+)\.", url)
        rank = int(m.group(1)) * int(m.group(2)) if m else 0
        seen[url] = {"url": url, "alt": alt.strip()[:120], "rank": rank}
    ranked = sorted(seen.values(), key=lambda d: d["rank"], reverse=True)
    for d in ranked:
        d.pop("rank", None)
    return ranked[:limit]


@router.post("/api/thailandnow/events/images")
async def event_images(payload: dict = Body(default={})):
    """Find related images for an event. Primary source: scrape the event's own
    page via Jina for embedded content images (highest signal — they depict the
    event). The Pexels/Pixabay stock fallback is deferred until their keys land
    in the service env; the event's own images are the better source anyway."""
    body = payload or {}
    event = body.get("event") or {}
    url = body.get("url") or event.get("url")
    images: list[dict] = []
    errors: list[str] = []
    if url:
        try:
            md = await _jina_read(url)
            images = _parse_images(md)
        except Exception as e:
            errors.append(f"scrape {url}: {e}")
    return {
        "images": images,
        "count": len(images),
        "errors": errors,
        "note": "event-page scrape only; add PEXELS/PIXABAY keys for a stock fallback",
    }


@router.post("/api/thailandnow/events/create")
async def create_event_doc(payload: dict = Body(default={})):
    """Create the TIAN Doc+card for a scouted event. The Doc body is the (edited)
    publicity bundle; the card desc carries the event URL(s) + image links, and the card
    gets start/due dates per ``_date_rule``. The titled name templates are passed as
    overrides so the desk's default (blank "Empty Event Document") stays for WRITERS."""
    body = payload or {}
    event = body.get("event") or {}
    image_urls = body.get("image_urls") or []
    urls = body.get("urls") or ([event["url"]] if event.get("url") else [])
    title = event.get("title", "Untitled Event")
    desc_lines = []
    if urls:
        desc_lines.append("Source: " + " ".join(urls))
    if image_urls:
        desc_lines.append("Images: " + " ".join(image_urls))
    # _date_rule → (start, due); the thick-box date inputs can override either.
    start_iso, due_iso = _date_rule(
        event.get("start_date"), event.get("end_date"), event.get("signup_deadline")
    )
    return await provision({
        "desk_id": "tian",
        "title": title,
        "body": body.get("bundle_text") or "",
        "card_desc": "\n".join(desc_lines),
        "doc_name": '[{yyyymm}] [EN] "{title}"',
        "card_name": "Event | {title}",
        "due": body.get("due") or due_iso,
        "start": body.get("start") or start_iso,
    })


if __name__ == "__main__":
    # Self-check the non-trivial pure logic (no network, no creds).
    # R5: LLM-JSON parsing + date normalization / window filtering.
    assert _parse_json_lenient('```json\n[{"a": 1}]\n```') == [{"a": 1}], "fenced json"
    assert _parse_json_lenient('sure! [{"a": 1}] here') == [{"a": 1}], "json in prose"
    assert _parse_json_lenient("no json here") is None, "non-json -> None"
    assert _iso_date("2026-08-15") == "2026-08-15" and _iso_date("2026-08-15T10:00:00Z") == "2026-08-15"
    assert _iso_date("not a date") is None and _iso_date(None) is None
    nev = _normalize_event({"title": "X", "start_date": "2026-08-10"}, "2026-08-01", "2026-08-31")
    assert nev and nev["start_date"] == "2026-08-10", nev
    assert _normalize_event({"title": "X", "start_date": "2025-01-01"}, "2026-08-01", "2026-08-31") is None, "past filtered"
    assert _normalize_event({"title": "X"}, "2026-08-01", "2026-08-31") is None, "no date filtered"
    ddg = _parse_ddg(
        "## [All Conference Alert](https://duckduckgo.com/l/?uddg=https%3A%2F%2Fallconferencealert.net%2Fx)"
    )
    assert ddg and ddg[0]["url"] == "https://allconferencealert.net/x", ddg
    gem = _load_gem(_gem_path())
    assert "## Role & Purpose" in gem and "Output Layout" in gem, "gem extraction wrong"
    assert not gem.startswith("---"), "frontmatter not stripped"
    imgs = _parse_images(
        "![hero](https://x/wp-content/uploads/2026/07/run-1024x682.jpg)"
        " ![icon](https://x/icon-hamburger.svg)"
        " ![logo](https://x/logo-150x150.png)"
        " ![gallery](https://x/run-300x200.webp)"
    )
    assert len(imgs) == 2, imgs  # svg not matched; logo skipped; 2 raster kept
    assert imgs[0]["url"].endswith("run-1024x682.jpg"), imgs[0]  # largest crop ranks first
    # name resolution + #NN dedup
    assert _resolve_name("[{yyyymm}] [CAT] #{nn}", "202607", "JUL", 3, None) == "[202607] [CAT] #03"
    assert _resolve_name("Article | {mon} #{nn}", "202607", "JUL", 12, None) == "Article | JUL #12"
    assert _next_nn("[{yyyymm}] [CAT] #{nn}", "202607", "JUL", None,
                    ["[202607] [CAT] #01", "[202607] [CAT] #07", "[202605] [CAT] #09"]) == 8
    assert _next_nn("[{yyyymm}] [CAT] #{nn}", "202607", "JUL", None, []) == 1
    # R1: month override
    assert _mon_for("202608") == "AUG"
    assert _mon_for("202613") is None and _mon_for("garbage") is None
    # R3: card date rule (start, due)
    assert _date_rule("2026-08-10", "2026-08-12", None) == ("2026-08-10T00:00:00.000Z", "2026-08-12T00:00:00.000Z"), "multi-day → start+due"
    assert _date_rule("2026-08-10", None, None) == (None, "2026-08-10T00:00:00.000Z"), "single-day → due only"
    assert _date_rule("2026-08-25", None, "2026-08-20") == ("2026-08-18T00:00:00.000Z", "2026-08-25T00:00:00.000Z"), "signup → start=due−7"
    assert _date_rule(None, None, None) == (None, None), "no dates → nothing"
    ym, mon = _yyyymm_mon()
    print("OK parsers — ddg:", [d["url"] for d in ddg], "| json+date normalize OK")
    print("OK gem:", len(gem), "chars | images:", [i["url"].split("/")[-1] for i in imgs])
    print("OK names + dedup (yyyymm", ym, mon, ")")
