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

import re
import urllib.parse
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


if __name__ == "__main__":
    # Self-check the non-trivial parser + gem-extraction + image logic (no network).
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
    print("OK parsers — tn:", titles, "| ddg:", [d["url"] for d in ddg])
    print("OK gem:", len(gem), "chars, starts:", repr(gem[:40]))
    print("OK images:", [i["url"].split("/")[-1] for i in imgs])
