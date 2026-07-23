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

import json
import os
import re
import urllib.parse
from datetime import datetime
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
    yyyymm, mon = _yyyymm_mon()
    title = body.get("title")
    doc_body = body.get("body") or ""
    card_desc = body.get("card_desc") or ""

    list_id = await _trello_list_id(board, desk["trello_list_name"])
    cards = await _trello("GET", f"/lists/{list_id}/cards", {"fields": "name"})
    nn = _next_nn(desk["card_name"], yyyymm, mon, title, [c["name"] for c in cards])

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
        doc_name = _resolve_name(desk["doc_name"], yyyymm, mon, cur, title)
        card_name = _resolve_name(desk["card_name"], yyyymm, mon, cur, title)
        doc_url = await _google_create_doc(token, desk["drive_folder_id"], doc_name, doc_body)
        card = await _trello("POST", "/cards", {"idList": list_id, "name": card_name, "desc": card_desc})
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


# Event links on Naz's own site follow /event/<slug>/ — clean [Title](url) pairs.
_TN_EVENT_RE = re.compile(
    r"\[([^\]]+)\]\((https://www\.thailandnow\.in\.th/event/[^)\s#]+)\)"
)


def _parse_tn_events(md: str) -> list[dict]:
    """Extract {title,url} entries from the thailandnow.in.th/events listing.

    Jina truncates the image-wrapping markdown (no closing paren) so those links
    don't match — only the clean ``[Title](event-url)`` pairs do. Dedupe by URL."""
    out: dict[str, dict] = {}
    for title, url in _TN_EVENT_RE.findall(md):
        title = title.strip()
        if not title or title.lower().startswith("image"):
            continue
        url = url.split("#")[0].rstrip("/")
        out.setdefault(
            url,
            {"title": title, "url": url, "date": "", "location": "", "source": "thailandnow"},
        )
    return list(out.values())


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


@router.post("/api/thailandnow/events/scout")
async def scout_events(payload: dict = Body(default={})):
    """Events radar. Primary source is Naz's own site events archive (clean,
    high-signal — exactly TIAN's beat). A free-text ``query`` adds a DuckDuckGo
    pass via Jina for broader coverage. No API keys needed.

    The listing carries no per-event dates, so ``weeks`` only shapes the DDG
    query phrasing — precise date filtering happens at the detail-fetch step
    (``/events/publicize``) once an event is picked."""
    body = payload or {}
    query = (body.get("query") or "").strip()
    weeks = body.get("weeks") or 4
    events: dict[str, dict] = {}
    errors: list[str] = []
    try:
        md = await _jina_read("https://www.thailandnow.in.th/events")
        for ev in _parse_tn_events(md):
            events[ev["url"]] = ev
    except Exception as e:  # network/parse failure — keep going, report it
        errors.append(f"thailandnow events: {e}")
    if query:
        try:
            q = f"Thailand {query} next {weeks} weeks conference seminar culture festival"
            md = await _jina_read(f"https://duckduckgo.com/html/?q={urllib.parse.quote(q)}")
            for ev in _parse_ddg(md):
                events.setdefault(ev["url"], ev)
        except Exception as e:
            errors.append(f"duckduckgo: {e}")
    return {"events": list(events.values()), "count": len(events), "errors": errors}


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
    publicity bundle; the card desc carries the event URL(s) + chosen image links.
    Just a tian-desk provision with a non-empty body — same engine as WRITERS."""
    body = payload or {}
    event = body.get("event") or {}
    image_urls = body.get("image_urls") or []
    urls = body.get("urls") or ([event["url"]] if event.get("url") else [])
    desc_lines = []
    if urls:
        desc_lines.append("Source: " + " ".join(urls))
    if image_urls:
        desc_lines.append("Images: " + " ".join(image_urls))
    return await provision({
        "desk_id": "tian",
        "title": event.get("title", "Untitled Event"),
        "body": body.get("bundle_text") or "",
        "card_desc": "\n".join(desc_lines),
    })


if __name__ == "__main__":
    # Self-check the non-trivial pure logic (no network, no creds).
    sample = (
        "[![Image 17](https://www.thailandnow.in.th/wp-content/uploads/2026/07/x.jpeg)\n"
        "[Thailand-China Cooperation Expo 2026](https://www.thailandnow.in.th/event/thailand-china-cooperation-expo/)\n"
        "[ULTRAMAN HERO RUN 2026](https://www.thailandnow.in.th/event/ultraman-hero-run/)\n"
        "[Thailand-China Cooperation Expo 2026](https://www.thailandnow.in.th/event/thailand-china-cooperation-expo/)\n"
    )
    ev = _parse_tn_events(sample)
    titles = [e["title"] for e in ev]
    assert len(ev) == 2, f"dedupe failed: got {len(ev)}"
    assert "ULTRAMAN HERO RUN 2026" in titles, titles
    assert all(e["url"].startswith("https://www.thailandnow.in.th/event/") for e in ev)
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
    ym, mon = _yyyymm_mon()
    print("OK parsers — tn:", titles, "| ddg:", [d["url"] for d in ddg])
    print("OK gem:", len(gem), "chars | images:", [i["url"].split("/")[-1] for i in imgs])
    print("OK names + dedup (yyyymm", ym, mon, ")")
