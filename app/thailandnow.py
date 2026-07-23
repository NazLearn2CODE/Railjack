"""Thailand NOW — monthly content-pipeline module (WRITERS + EVENTS).

Two halves share one desk-driven create/attach engine:
  WRITERS — bulk-create blank Google Docs + Trello cards per writer (Paul/Teerin).
  EVENTS  — scout upcoming Thailand events, generate a publicity bundle, then
            spin up a prefilled Doc + card (TIAN desk).

Desks (Paul/Teerin/TIAN) are config rows in configs/tawhan.yaml → options.desks,
so adding/changing a writer is a YAML edit, not code. REST-only via httpx — no
google-api-python-client, no py-trello. See docs/thailandnow-plan.md.
"""

from __future__ import annotations

import re
import urllib.parse

import httpx
from fastapi import APIRouter, Body

from .config import CONFIG

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


if __name__ == "__main__":
    # Self-check the non-trivial parser logic on a captured snippet (no network).
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
    print("OK parsers — tn:", titles, "| ddg:", [d["url"] for d in ddg])
