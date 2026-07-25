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
import signal
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

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


# --- DEEP mode async job store (copies notebooklm.py's _JOBS/_spawn pattern) ---
# Module-local (not shared) — matches notebooklm/comfyui/ffmpeg house style.


@dataclass
class TnJob:
    id: str
    kind: str  # "deep-search" (research) — "deep-extract" may follow
    label: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: int = 0
    events: list[dict] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    window: dict | None = None
    notebook: str | None = None       # nid
    notebook_title: str | None = None
    error: str | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=200))
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    cancel: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "progress": self.progress,
            "events": self.events, "source_urls": self.source_urls,
            "window": self.window, "notebook": self.notebook,
            "notebook_title": self.notebook_title,
            "error": self.error, "logs": list(self.logs),
        }


_TN_JOBS: dict[str, TnJob] = {}
_TN_RUNNING = {"queued", "running"}
_TN_BG: set[asyncio.Task[object]] = set()


class _TnCancelled(Exception):
    """Internal: a job step saw job.cancel (or was SIGTERM'd) mid-run."""


async def _tn_run_step(job: TnJob, argv: list[str], timeout: float) -> str:
    """One job step: run, stream stdout+stderr into job.logs, return stdout text.
    Raises _TnCancelled if cancelled/SIGTERM'd, else RuntimeError on failure —
    _tn_run_job maps both to job status. (Raises RuntimeError, NOT HTTPException,
    so background failures land in job.error instead of leaking to a request.)"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise RuntimeError("notebooklm CLI not found")
    job.proc = proc

    async def pump(stream) -> list[str]:
        buf: list[str] = []
        while True:
            line = await stream.readline()
            if not line:
                break
            s = line.decode(errors="replace").rstrip()
            job.logs.append(s)
            buf.append(s)
        return buf

    try:
        out, err = await asyncio.wait_for(
            asyncio.gather(pump(proc.stdout), pump(proc.stderr)), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        job.proc = None
        raise RuntimeError(f"step timed out: {' '.join(argv[1:4])}")
    rc = await proc.wait()
    job.proc = None
    if job.cancel or rc in (-signal.SIGTERM, -signal.SIGINT):
        raise _TnCancelled()
    if rc != 0:
        raise RuntimeError("\n".join(err).strip() or f"exit {rc}")
    return "\n".join(out)


async def _tn_run_job(job: TnJob, flow) -> None:
    """Wrap a flow coroutine: running → done/error/cancelled."""
    job.status = "running"
    try:
        await flow
    except _TnCancelled:
        job.status = "cancelled"
        return
    except Exception as e:  # noqa: BLE001 — any step failure → error status
        job.status, job.error = "error", str(e) or e.__class__.__name__
        return
    if job.cancel:
        job.status = "cancelled"
    else:
        job.status, job.progress = "done", 100


def _tn_spawn(kind: str, label: str, make_flow) -> dict:
    """Create a queued TnJob and schedule its flow (a job -> coroutine factory)."""
    jid = uuid4().hex[:8]
    job = TnJob(id=jid, kind=kind, label=label)
    _TN_JOBS[jid] = job
    task = asyncio.create_task(_tn_run_job(job, make_flow(job)))
    _TN_BG.add(task)
    task.add_done_callback(_TN_BG.discard)
    return {"id": jid}


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


# --- FREE extraction: regex-based event parsing (no LLM, 100% free) ---
# ponytail: SCOUT is now regex-only; z.ai lives only in DEEP mode (2026-07-24)


# Thai month names + abbreviations (for Buddhist-era year support)
_THAI_MONTHS = [
    "ม.ค.", "มกราคม", "ม.ค.", "January",
    "ก.พ.", "กุมภาพันธ์", "ก.พ.", "February",
    "มี.ค.", "มีนาคม", "มี.ค.", "March",
    "เม.ย.", "เมษายน", "เม.ย.", "April",
    "พ.ค.", "พฤษภาคม", "พ.ค.", "May",
    "มิ.ย.", "มิถุนายน", "มิ.ย.", "June",
    "ก.ค.", "กรกฎาคม", "ก.ค.", "July",
    "ส.ค.", "สิงหาคม", "ส.ค.", "August",
    "ก.ย.", "กันยายน", "ก.ย.", "September",
    "ต.ค.", "ตุลาคม", "ต.ค.", "October",
    "พ.ย.", "พฤศจิกายน", "พ.ย.", "November",
    "ธ.ค.", "ธันวาคม", "ธ.ค.", "December",
]

# Date regex patterns (EN DMY/MDY, ISO, Thai with Buddhist era)
_DATE_PATTERNS = [
    # ISO: 2026-07-15 or 2026/07/15
    r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b",
    # EN DMY: 15 July 2026 / 15th July 2026 / 15-July-2026
    r"\b(\d{1,2})(?:st|nd|rd|th)?[-\s]+([A-Za-z]+)[-\s]+(\d{4})\b",
    # EN MDY: July 15, 2026 / July 15th 2026
    r"\b([A-Za-z]+)[-\s]+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\b",
    # Thai Buddhist: 15 ก.ค. 2569 (convert 2569 → 2026)
    r"\b(\d{1,2})[-\s]+(" + "|".join(_THAI_MONTHS[:12]) + r")[-\s]+(\d{4})\b",
]

_MONTH_NAMES = {
    "january": "01", "jan": "01", "february": "02", "feb": "02",
    "march": "03", "mar": "03", "april": "04", "apr": "04", "may": "05",
    "june": "06", "jun": "06", "july": "07", "jul": "07", "august": "08", "aug": "08",
    "september": "09", "sep": "09", "october": "10", "oct": "10",
    "november": "11", "nov": "11", "december": "12", "dec": "12",
}

_THAI_TO_EN_MONTH = {
    "มกราคม": "01", "ม.ค.": "01", "กุมภาพันธ์": "02", "ก.พ.": "02",
    "มีนาคม": "03", "มี.ค.": "03", "เมษายน": "04", "เม.ย.": "04",
    "พฤษภาคม": "05", "พ.ค.": "05", "มิถุนายน": "06", "มิ.ย.": "06",
    "กรกฎาคม": "07", "ก.ค.": "07", "สิงหาคม": "08", "ส.ค.": "08",
    "กันยายน": "09", "ก.ย.": "09", "ตุลาคม": "10", "ต.ค.": "10",
    "พฤศจิกายน": "11", "พ.ย.": "11", "ธันวาคม": "12", "ธ.ค.": "12",
}


def _parse_date(text: str) -> str | None:
    """Extract first YYYY-MM-DD date from text using multiple patterns.
    Supports EN DMY/MDY, ISO, and Thai Buddhist era (2569 → 2026)."""
    if not text:
        return None

    text_lower = text.lower()

    # Try ISO format first (e.g., 2026-07-15)
    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if m:
        year, month, day = m.groups()
        try:
            d = datetime(int(year), int(month), int(day))
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Try EN DMY: 15 July 2026
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?[-\s]+([a-z]+)[-\s]+(\d{4})\b", text_lower)
    if m:
        day, month_name, year = m.groups()
        month_num = _MONTH_NAMES.get(month_name[:3])
        if month_num:
            try:
                d = datetime(int(year), int(month_num), int(day))
                return d.strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Try EN MDY: July 15, 2026
    m = re.search(r"\b([a-z]+)[-\s]+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\b", text_lower)
    if m:
        month_name, day, year = m.groups()
        month_num = _MONTH_NAMES.get(month_name[:3])
        if month_num:
            try:
                d = datetime(int(year), int(month_num), int(day))
                return d.strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Try Thai Buddhist: 15 ก.ค. 2569 → convert to 2026-07-15
    for pat in _DATE_PATTERNS[3:4]:  # Only the Thai pattern
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            day, month_name, year = m.groups()
            month_num = _THAI_TO_EN_MONTH.get(month_name, _THAI_TO_EN_MONTH.get(month_name[:3]))
            if month_num and int(year) > 2400:  # Buddhist era
                year_ce = int(year) - 543
                try:
                    d = datetime(year_ce, int(month_num), int(day))
                    return d.strftime("%Y-%m-%d")
                except ValueError:
                    pass

    return None


def _extract_events_regex(markdown: str, source_url: str, today_iso: str,
                          window_end_iso: str, category: str) -> list[dict]:
    """Extract events from Jina-fetched markdown using regex only (no LLM).
    Finds title, URL, dates, location, language from page structure."""
    if not markdown:
        return []

    lines = markdown.split("\n")
    events: list[dict] = []

    # Look for event-like structures (headings + date patterns nearby)
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Heading = likely event title
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if len(title) > 200:
                title = title[:200]

            # Look ahead 5 lines AND back 5 lines for date + location (date often precedes title)
            date_found = None
            location = ""
            language = "th" if any(re.search(r"[ก-๙]", lines[j]) for j in range(i, min(i+6, len(lines)))) else "en"

            # lookback window: lines before the heading (date often comes first on listing pages)
            lookback_start = max(0, i - 5)
            lookback_lines = [lines[j].strip() for j in range(lookback_start, i)]
            # lookahead window: lines after the heading
            lookahead_lines = [lines[j].strip() for j in range(i+1, min(i+6, len(lines)))]

            for lookahead in [*lookback_lines, *lookahead_lines]:
                # Try to extract date
                if not date_found:
                    date_found = _parse_date(lookahead)

                # Simple location heuristics (common patterns)
                if not location and any(kw in lookahead.lower() for kw in
                                      ["bangkok", "bkk", "chiang mai", "phuket", "pattaya",
                                       "impact", "qsncc", "bitec", "ศูนย์", "จังหวัด", "กรุงเทพ", "เชียงใหม่"]):
                    location = lookahead[:120]

            # Only count if we have a valid date in window
            if date_found:
                if date_found < today_iso or date_found > window_end_iso:
                    i += 1
                    continue  # Skip events outside the window

                events.append({
                    "title": title,
                    "url": source_url,
                    "start_date": date_found,
                    "end_date": "",
                    "signup_deadline": "",
                    "location": location,
                    "language": language,
                    "summary": f"{category} event" if category else "Event from {source_url}",
                    "source": "scout",
                })

        i += 1

    return events


# --- LLM → structured JSON (R5): zai returns text; parse it defensively ---
# Only used in DEEP mode now; SCOUT is 100% free (regex)


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


async def _brave_urls(query: str, limit: int = 6) -> list[str]:
    """Brave Search result URLs — only when BRAVE_API_KEY is in the service env (R7 booster)."""
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": key},
                params={"q": query, "count": limit},
            )
            r.raise_for_status()
            data = r.json()
        return [w["url"] for w in (data.get("web") or {}).get("results", [])
                if isinstance(w, dict) and w.get("url")]
    except Exception:
        return []


async def _gnews_urls(query: str, limit: int = 6) -> list[str]:
    """GNews article URLs — only when GNEWS_KEY is in the env (R7 booster)."""
    key = os.environ.get("GNEWS_KEY")
    if not key:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://gnews.io/api/v4/search",
                params={"q": query, "max": limit, "lang": "en", "apikey": key},
            )
            r.raise_for_status()
            data = r.json()
        return [a["url"] for a in data.get("articles", []) if isinstance(a, dict) and a.get("url")]
    except Exception:
        return []


# Default scout sources (R5). Override per-machine via options.event_listings /
# options.event_queries in configs/<machine>.yaml. Keyless — TAT + aggregators + broad DDG.
_DEFAULT_LISTINGS = [
    "https://www.tourismthailand.vn/Events",
    "https://www.tourismthailand.org/Events",
    "https://allconferencealert.net/country/thailand/",
    "https://10times.com/thailand",
]


async def _extract_events_from(url: str, today_iso: str, window_end_iso: str, category: str,
                               source: str) -> tuple[list[dict], str | None]:
    """Jina-fetch one page, then extract events using regex only (NO LLM — 100% free).
    Supports EN DMY/MDY, ISO dates, and Thai Buddhist-era years. Returns (events, error_or_none)."""
    try:
        md = await _jina_read(url, timeout=30.0)
    except Exception as e:
        return [], f"fetch {url}: {e}"

    try:
        events = _extract_events_regex(md, url, today_iso, window_end_iso, category)
        return [e for e in events if e], None
    except Exception as e:
        return [], f"extract {url}: {e}"


@router.post("/api/thailandnow/events/scout")
async def scout_events(payload: dict = Body(default={})):
    """Events radar (R5) — future Thailand events only, multi-source, dated + filtered.

    **100% FREE (2026-07-24)**: Tier 1 / keyless — direct event-listing pages (TAT,
    allconferencealert, 10times) + DuckDuckGo broad queries (English + Thai), each
    fetched via **Jina + regex extraction only** — NO LLM calls in SCOUT. Supports
    EN DMY/MDY, ISO dates, and Thai Buddhist-era years (2569 → 2026). Results are
    window-filtered (start_date within today → today+weeks), deduped, and sorted by
    start_date. ``weeks`` is 1..52 (default 4). No API keys required.

    **Standing rule**: free-first search, paid LLM last resort. z.ai lives only in DEEP
    mode (/events/deep). See hot.md § 2026-07-24 for the Somatic→Railjack adoption brief.
    """
    body = payload or {}
    category = (body.get("query") or "").strip()
    weeks = max(1, min(52, int(body.get("weeks") or 4)))
    opts = _opts()
    today = datetime.now()
    window_end = today + timedelta(weeks=weeks)
    today_iso, window_end_iso = today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
    span = today.strftime("%B %Y")

    # candidate URLs: DuckDuckGo broad results FIRST (diverse sources), then direct listings
    errors: list[str] = []
    cat_q = f" {category}" if category else ""
    queries = opts.get("event_queries") or [
        # English queries (diverse source types: festival/exhibition/seminar/expo)
        f"Thailand events{cat_q} {span} conference festival exhibition",
        f"upcoming Thailand events{cat_q} {span} seminar expo Bangkok",
        # Thai queries (for Thai sources) - free-first rule
        f"อีเวนต์ ประเทศไทย{cat_q} {span} มหกรรม นิทรรศการ การประชุม เทศกาล",
        f"กิจกรรม{cat_q} {span} ไทย งานแสดงสินค้า ประชุม สัมมนา",
        # extra diversity: official + ticketing sites
        f"Thailand{cat_q} {span} events site:eventbrite.com OR site:tatnews.org",
    ]
    ddg_urls: list[str] = []
    for q in queries:
        try:
            md = await _jina_read(f"https://duckduckgo.com/html/?q={urllib.parse.quote(q)}")
            for ev in _parse_ddg(md):
                ddg_urls.append(ev["url"])
        except Exception as e:
            errors.append(f"ddg {q!r}: {e}")

    # R7: agent-reach pattern — run EVERY query through Brave (independent index, not Google-dependent)
    # + GNews for news coverage. All parallel, all best-effort (no-op without keys).
    brave_results = await asyncio.gather(*[_brave_urls(q) for q in queries])
    brave_urls = [u for batch in brave_results for u in batch]
    gnews_urls = await _gnews_urls(f"Thailand{cat_q} {span} events")
    ddg_urls.extend([*brave_urls, *gnews_urls])

    # dedupe by DOMAIN (1 URL per domain → no 10times.com spam, forces diversity)
    listings = opts.get("event_listings") or _DEFAULT_LISTINGS
    seen_domains: set[str] = set()
    urls: list[str] = []
    for u in [*ddg_urls, *listings]:  # DDG first (diverse), listings as fallback
        try:
            host = urllib.parse.urlparse(u).hostname or ""
            # strip www. + take last 2 labels for domain (e.g. 10times.com)
            domain = ".".join(host.replace("www.", "").split(".")[-2:])
        except Exception:
            domain = u
        if domain and domain not in seen_domains:
            seen_domains.add(domain)
            urls.append(u)
        if len(urls) >= 15:
            break
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


# --- EVENTS radar Tier 2 (R6): NotebookLM deep web research ---


def _nlm_notebook_id_path() -> Path:
    """Sidecar holding the dedicated notebook id (persists across restarts without mutating
    the version-controlled YAML)."""
    return Path(os.path.expanduser("~/.config/railjack/thailandnow_notebook.id"))


async def _nlm_run(args: list[str], timeout: float = 200.0) -> str:
    """Run the notebooklm CLI (authed once via `notebooklm login`), return stdout. Every call
    passes --notebook so we never touch the shared context.json (the race the skill warns of)."""
    proc = await asyncio.create_subprocess_exec(
        "notebooklm", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, f"notebooklm {' '.join(args[:2])} timed out ({timeout:.0f}s)")
    out = stdout.decode(errors="replace")
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        if "auth" in err.lower() or "login" in err.lower() or "cookie" in err.lower():
            raise HTTPException(501, "NotebookLM not authed — run `notebooklm login`, then retry")
        raise HTTPException(502, f"notebooklm exit {proc.returncode}: {err[:200]}")
    return out


async def _nlm_ensure_notebook() -> str:
    """Return the dedicated notebook id, creating it once on first use."""
    p = _nlm_notebook_id_path()
    if p.exists():
        nid = p.read_text().strip()
        if nid:
            return nid
    data = _parse_json_lenient(await _nlm_run(["create", "Thailand NOW Events Radar", "--json"])) or {}
    nid = data.get("id") or (data.get("notebook") or {}).get("id")  # CLI nests under "notebook"
    if not nid:
        raise HTTPException(502, "notebooklm create returned no id — check `notebooklm list`")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(nid)
    return nid


@router.get("/api/thailandnow/debug")
async def debug_info() -> dict:
    """Debug endpoint to check module health."""
    import traceback
    status: dict[str, str | bool] = {"module_loaded": True}

    # Test notebooklm CLI
    try:
        result = await _nlm_run(["auth", "check", "--json"], timeout=15)
        status["notebooklm_auth"] = "ok" if result else "failed"
    except Exception as e:
        status["notebooklm_auth"] = f"error: {e}"

    # Test z.ai (if used)
    try:
        from .zai import zai_message
        test = await zai_message("test", max_tokens=10, timeout=10)
        status["zai"] = "ok"
    except Exception as e:
        status["zai"] = f"error: {e}"

    return status

# --- EVENTS radar DEEP mode: async research (per-run notebook) + on-demand extract ---
# Two-button model: SEARCH kicks off NotebookLM research in the background (browser-
# notifies on done); EXTRACT runs the free regex extractor on a chosen notebook's
# sources whenever the user likes. No z.ai (quota exhausted). Copies notebooklm.py's
# _JOBS/_spawn background-job pattern (see _tn_spawn above).

_TN_NB_PREFIX = "Thailand NOW Events"  # notebooks this mode creates are filtered by this prefix


async def _tn_list_notebooks() -> list[dict]:
    """notebooklm list, filtered to only this mode's notebooks (title prefix match).
    Adds a ready-source count per notebook for the dropdown."""
    out = await _nlm_run(["list", "--json"], timeout=30)
    data = _parse_json_lenient(out) or {}
    nbs = data.get("notebooks", data if isinstance(data, list) else [])
    mine = [n for n in nbs if str(n.get("title", "")).startswith(_TN_NB_PREFIX)]
    # newest first (created_at desc) — most recent research surfaces at top of dropdown
    mine.sort(key=lambda n: str(n.get("created_at") or ""), reverse=True)
    return mine


async def _tn_source_urls(nid: str) -> list[str]:
    """Ready source URLs for a notebook (status == 'ready')."""
    out = await _nlm_run(["source", "list", "--json", "--notebook", nid], timeout=30)
    data = _parse_json_lenient(out) or {}
    srcs = data.get("sources", data if isinstance(data, list) else [])
    return [s["url"] for s in srcs
            if isinstance(s, dict) and s.get("status") == "ready" and s.get("url")]


async def _flow_deep_search(job: TnJob, category: str, weeks: int,
                            today_iso: str, window_end_iso: str, span: str) -> None:
    """Background flow: create a fresh per-run notebook → start research → wait for it
    (up to 600s, IN THE BACKGROUND not in the HTTP request) → collect ready source URLs.
    No extraction here — EXTRACT is a separate on-demand call. Research RPC failing
    upstream (START_FAST_RESEARCH null) is caught + logged, then we still scan whatever
    sources the notebook already has."""
    job.window = {"from": today_iso, "to": window_end_iso, "weeks": weeks}
    topic = re.sub(r"\s+", " ", f"upcoming and future Thailand {category} events "
                              f"happening AFTER {today_iso}, within the next {weeks} weeks — "
                              "conference festival exhibition seminar").strip()
    title = f"{_TN_NB_PREFIX} · {span} · {today_iso}"

    # 1. create a fresh notebook for this run
    out = await _tn_run_step(job, ["notebooklm", "create", title, "--json"], timeout=60)
    data = _parse_json_lenient(out) or {}
    nid = data.get("id") or (data.get("notebook") or {}).get("id")
    if not nid:
        raise RuntimeError("notebooklm create returned no id")
    job.notebook, job.notebook_title = nid, title
    job.progress = 10

    # 2-3. start research + wait for it (the long step, runs in background)
    try:
        await _tn_run_step(job, ["notebooklm", "source", "add-research", topic,
                                 "--mode", "fast", "--no-wait", "--notebook", nid], timeout=120)
        job.progress = 30
        await _tn_run_step(job, ["notebooklm", "research", "wait", "-n", nid,
                                 "--import-all", "--timeout", "600", "--json"], timeout=660)
        job.progress = 90
    except _TnCancelled:
        raise  # let _tn_run_job mark cancelled
    except Exception as e:
        # START_FAST_RESEARCH currently returns null upstream — log + continue to source scan
        job.logs.append(f"research step failed (continuing to source export): {e}")

    # 4. collect ready source URLs (always run, even if research failed)
    try:
        job.source_urls = await _tn_source_urls(nid)
    except Exception as e:
        job.logs.append(f"source list failed: {e}")
    # _tn_run_job bumps progress to 100 on clean return


@router.post("/api/thailandnow/deep/search")
async def deep_search(payload: dict = Body(default={})):
    """DEEP SEARCH — kick off NotebookLM research in the background. Returns {id}
    immediately; poll GET /api/thailandnow/jobs. Browser-notifies on done (frontend).
    One deep-search job at a time (409 if already queued/running)."""
    if any(j.kind == "deep-search" and j.status in _TN_RUNNING for j in _TN_JOBS.values()):
        raise HTTPException(409, "a DEEP SEARCH job is already queued/running")
    body = payload or {}
    category = (body.get("query") or "").strip()
    weeks = max(1, min(52, int(body.get("weeks") or 4)))
    today = datetime.now()
    window_end = today + timedelta(weeks=weeks)
    today_iso, window_end_iso = today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
    span = today.strftime("%B %Y")
    label = f"DEEP · {category or 'events'} · {today_iso}→{window_end_iso}"
    return _tn_spawn("deep-search", label,
                     lambda j: _flow_deep_search(j, category, weeks, today_iso, window_end_iso, span))


@router.post("/api/thailandnow/deep/extract")
async def deep_extract(payload: dict = Body(default={})):
    """DEEP EXTRACT — on-demand: run the FREE regex extractor on a chosen notebook's
    ready sources → dated events. No z.ai. Same response shape as /events/scout so the
    frontend merge works unchanged. Press whenever (research need not be running)."""
    body = payload or {}
    nid = (body.get("notebook_id") or "").strip()
    if not nid:
        raise HTTPException(400, "notebook_id required")
    category = (body.get("query") or "").strip()
    weeks = max(1, min(52, int(body.get("weeks") or 4)))
    today = datetime.now()
    window_end = today + timedelta(weeks=weeks)
    today_iso, window_end_iso = today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
    window = {"from": today_iso, "to": window_end_iso, "weeks": weeks}
    errors: list[str] = []

    try:
        source_urls = await _tn_source_urls(nid)
    except Exception as e:
        return {"events": [], "count": 0, "errors": [f"sources fetch failed: {e}"],
                "window": window, "notebook": nid}
    if not source_urls:
        return {"events": [], "count": 0, "errors": ["notebook has no ready sources"],
                "window": window, "notebook": nid}

    events: dict[str, dict] = {}
    results = await asyncio.gather(
        *[_extract_events_from(u, today_iso, window_end_iso, category, "deep") for u in source_urls[:20]],
        return_exceptions=True,
    )
    for r in results:
        if isinstance(r, Exception):
            errors.append(f"extract: {r}")
            continue
        evs, err = r
        if err:
            errors.append(err)
        for ev in evs:
            n = _normalize_event(ev, today_iso, window_end_iso, "deep")
            if n:
                events.setdefault(n["url"] or n["title"], n)
    ordered = sorted(events.values(), key=lambda e: e.get("start_date") or "9999")
    return {"events": ordered, "count": len(ordered), "errors": errors,
            "window": window, "notebook": nid}


@router.post("/api/thailandnow/deep/seed")
async def deep_seed(payload: dict = Body(default={})):
    """Seed a ThickBox event from a single source URL: Jina-fetch + free regex
    extract (title/dates/location). Used by the notebook-browser checkbox → open
    the event detail panel for one URL at a time. Wide window (52w) so seed never
    drops an event just for being outside the scouted span. Falls back to a
    URL-derived title if the page has no parseable dated event."""
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    today = datetime.now()
    window_end = today + timedelta(weeks=52)
    today_iso, window_end_iso = today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")
    evs, _err = await _extract_events_from(url, today_iso, window_end_iso, "", "deep-seed")
    if evs:
        return {"event": evs[0]}
    # fallback: no dated event found — derive a title from the URL host+path
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        title = host.replace("www.", "") or url
    except Exception:
        title = url[:80]
    return {"event": {
        "title": title, "url": url, "start_date": "", "end_date": "",
        "signup_deadline": "", "location": "", "language": "en",
        "summary": "", "source": "deep-seed",
    }}


@router.get("/api/thailandnow/deep/notebooks")
async def deep_notebooks() -> dict:
    """This mode's notebooks (title prefix 'Thailand NOW Events'), newest first.
    Source of the EXTRACT dropdown."""
    return {"notebooks": await _tn_list_notebooks()}


@router.get("/api/thailandnow/deep/notebooks/{nid}/sources")
async def deep_notebook_sources(nid: str) -> dict:
    """All sources of a notebook (for browsing URLs). Unlike _tn_source_urls (ready-only),
    returns every source with its status so the user can see what's importable."""
    out = await _nlm_run(["source", "list", "--json", "--notebook", nid], timeout=30)
    data = _parse_json_lenient(out) or {}
    srcs = data.get("sources", data if isinstance(data, list) else [])
    return {"sources": [
        {"id": s.get("id", ""), "title": s.get("title", ""),
         "url": s.get("url", ""), "status": s.get("status", "")}
        for s in srcs if isinstance(s, dict)
    ]}


@router.get("/api/thailandnow/jobs")
def tn_jobs() -> dict:
    """All Thailand NOW jobs (deep-search, …) with full status — polled by the frontend."""
    return {"jobs": [j.to_dict() for j in reversed(list(_TN_JOBS.values()))]}


@router.post("/api/thailandnow/jobs/{jid}/cancel")
async def tn_cancel_job(jid: str) -> dict:
    j = _TN_JOBS.get(jid)
    if not j:
        raise HTTPException(404, "unknown job")
    if j.status not in _TN_RUNNING:
        raise HTTPException(409, f"job is {j.status}, nothing to cancel")
    j.cancel = True
    if j.proc is not None:
        j.proc.send_signal(signal.SIGTERM)
    return {"status": "cancelling"}


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


# --- ARCHIVE tab (Stage 1) — chat Q&A over the Event Drive, DIRECT path only ---
# Stage 1 answers presence/listing + title-search questions directly (no LLM).
# Detail/synthesis questions ("when is Songkran?") 501 until Stage 2 wires z.ai.
# z.ai is NOT called here at all — direct retrieval only. See docs/thailandnow-plan.md.

_ARCHIVE_LIST_CACHE: list[dict] | None = None
_ARCHIVE_LIST_CACHE_AT: float = 0.0
_ARCHIVE_LIST_TTL_S: float = 90.0

# Stopwords stripped before deciding list-all vs specific lookup. Includes the
# presence phrases themselves ("do we have", "lined up", "any", "what events",
# "coming up", "list", "show me") + function words + "event(s)" — so "do we have
# any events lined up?" leaves no content tokens (→ list-all), while "do we have
# Songkran lined up?" leaves "songkran" (→ targeted title search).
_ARCHIVE_STOP = {
    "do", "we", "have", "has", "had", "any", "some", "lined", "up", "for", "from",
    "the", "a", "an", "in", "on", "at", "is", "are", "was", "were", "there", "here",
    "show", "me", "us", "all", "list", "what", "whats", "coming", "of", "about",
    "please", "events", "event", "this", "that", "next", "upcoming", "scheduled",
    "planned",
}

# synthesis/detail intent → wants specific info about an event. Stage 1 can't
# answer these (no LLM), so they 501. Interrogatives + "tell me about/describe".
_ARCHIVE_DETAIL = re.compile(
    r"\b(when|where|who|why|how|what date|what time|how much|how many|"
    r"tell me about|describe)\b"
)


def _archive_folder_id() -> str:
    """The Event Drive folder: options.archive_folder_id if set, else the TIAN
    desk's drive_folder_id (where EVENTS→CREATE writes the [yyyymm] docs). 503
    if neither is configured."""
    opts = _opts()
    fid = opts.get("archive_folder_id")
    if fid:
        return fid
    desk = next((d for d in opts.get("desks", []) if d.get("id") == "tian"), None)
    if desk and desk.get("drive_folder_id"):
        return desk["drive_folder_id"]
    raise HTTPException(
        503, "no Event Drive folder — set options.archive_folder_id "
        "or a tian desk drive_folder_id",
    )


async def _drive_list_event_docs(token: str) -> list[dict]:
    """List the Event Drive's Google Docs (newest first), paginating on
    nextPageToken, 90s-TTL-cached. Returns [{id,name,modifiedTime,webViewLink}].
    The Drive ``q`` is passed via httpx params= (folder id folded into the q
    value, never the URL)."""
    global _ARCHIVE_LIST_CACHE, _ARCHIVE_LIST_CACHE_AT
    if _ARCHIVE_LIST_CACHE is not None and \
            (time.monotonic() - _ARCHIVE_LIST_CACHE_AT) < _ARCHIVE_LIST_TTL_S:
        return _ARCHIVE_LIST_CACHE
    folder = _archive_folder_id()
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    out: list[dict] = []
    page_token: str | None = None
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            params = {
                "q": f"'{folder}' in parents and "
                "mimeType='application/vnd.google-apps.document' and trashed=false",
                "fields": "files(id,name,modifiedTime,webViewLink),nextPageToken",
                "orderBy": "modifiedTime desc",
                "pageSize": 200,
                "supportsAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            r = await c.get("https://www.googleapis.com/drive/v3/files",
                            headers=hdr, params=params)
            if r.status_code != 200:
                raise HTTPException(502, f"Drive list failed: {r.status_code} {r.text[:200]}")
            data = r.json()
            for f in data.get("files", []):
                out.append({
                    "id": f.get("id", ""), "name": f.get("name", ""),
                    "modifiedTime": f.get("modifiedTime", ""),
                    "webViewLink": f.get("webViewLink", ""),
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    _ARCHIVE_LIST_CACHE = out
    _ARCHIVE_LIST_CACHE_AT = time.monotonic()
    return out


async def _drive_read_doc(token: str, doc_id: str) -> str:
    """Export a Google Doc as plain text. (Stage 2 calls this to feed synthesis;
    Stage 1 defines but does not call it.)"""
    hdr = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://www.googleapis.com/drive/v3/files/{doc_id}/export",
            headers=hdr, params={"mimeType": "text/plain"},
        )
        if r.status_code != 200:
            raise HTTPException(502, f"Doc export failed: {r.status_code} {r.text[:200]}")
        return r.text


def _archive_tokenize(text: str) -> set[str]:
    """Lowercase alnum tokens ≥2 chars. Strips [], quotes — only alnum runs survive."""
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 2}


def _archive_score_title(q_tokens: set[str], title: str) -> int:
    """Relevance of a query against a doc title: substring-hit ×3 (token of len≥3
    found anywhere in the title) + exact token-overlap ×1. 0 if nothing overlaps."""
    t_low = title.lower()
    score = 0
    for q in q_tokens:
        if len(q) >= 3 and q in t_low:
            score += 3
    score += len(q_tokens & _archive_tokenize(t_low))
    return score


def _archive_content_tokens(question: str) -> set[str]:
    """Meaningful query tokens: tokenize, drop a bare 20xx year + stopwords.
    Empty ⇒ general listing request (→ list-all); non-empty ⇒ specific lookup
    (→ title search those tokens)."""
    toks = _archive_tokenize(question)
    year_m = re.search(r"\b(20\d{2})\b", question)
    if year_m:
        toks.discard(year_m.group(1))
    return toks - _ARCHIVE_STOP


def _archive_is_presence(question: str) -> bool:
    """General listing intent (no specific event named) → list-all response.
    True when no content tokens survive stripping (e.g. 'do we have any events?',
    'what's coming up?', '2026'). 'do we have Songkran?' leaves 'songkran' → False."""
    return not _archive_content_tokens(question)


def _archive_is_detail(question: str) -> bool:
    """Synthesis/detail intent — wants specific info about an event (when/where/
    how much). Stage 1 has no LLM, so these 501 until Stage 2 wires the path."""
    return bool(_ARCHIVE_DETAIL.search(question.lower()))


@router.post("/api/thailandnow/archive/ask")
async def archive_ask(payload: dict = Body(default={})):
    r"""ARCHIVE tab (Stage 1) — chat Q&A over the Event Drive. DIRECT answers only:

    presence/listing → all names + links (cap 30, newest first);
    title search → tokenize + score doc names (year-filtered on a \b20\d{2}\b in
    the question), matches as "• name — link", else 3 closest titles + folder link;
    detail/synthesis → 501 (Stage 2 wires z.ai). No LLM call here at all."""
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question required")
    token = await _google_token()
    docs = await _drive_list_event_docs(token)
    folder = _archive_folder_id()
    folder_link = f"https://drive.google.com/drive/folders/{folder}"

    if not docs:
        return {"answer": "No event docs in the Event Drive yet.",
                "sources": [], "mode": "direct"}

    # 1) detail / synthesis → Stage 2 (no LLM in Stage 1). Checked FIRST so a
    #    question like "when is the next event?" 501s instead of list-all.
    if _archive_is_detail(question):
        raise HTTPException(501, "synthesis path not yet wired (Stage 2)")

    # 2) general listing (no specific event named) → all names + links
    if _archive_is_presence(question):
        top = docs[:30]
        return {
            "answer": "\n".join(f"• {d['name']} — {d['webViewLink']}" for d in top),
            "sources": [{"name": d["name"], "url": d["webViewLink"]} for d in top],
            "mode": "direct",
        }

    # 3) specific lookup → title search the content tokens (year-filtered)
    content = _archive_content_tokens(question)
    year_m = re.search(r"\b(20\d{2})\b", question)
    pool = docs
    if year_m:
        year = year_m.group(1)
        pool = [d for d in docs if re.match(rf"^\[{year}", d["name"])] or docs
    scored = sorted(
        ((_archive_score_title(content, d["name"]), d) for d in pool),
        key=lambda x: x[0], reverse=True,
    )
    matches = [d for s, d in scored if s > 0]
    if matches:
        return {
            "answer": "\n".join(f"• {d['name']} — {d['webViewLink']}" for d in matches),
            "sources": [{"name": d["name"], "url": d["webViewLink"]} for d in matches],
            "mode": "direct",
        }
    # no match → 3 closest titles + folder link
    sugg = [d["name"] for _, d in scored[:3]]
    answer = f"No events matching `{question}` in the Event Drive."
    if sugg:
        answer += "\n\nClosest titles:\n" + "\n".join(f"• {n}" for n in sugg)
    answer += f"\n\nBrowse the Event Drive: {folder_link}"
    return {"answer": answer, "sources": [], "mode": "direct"}


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
    # ARCHIVE Stage 1: tokenize + title score + presence/detail classification
    assert _archive_tokenize('Songkran 2026 "[EN]"') == {"songkran", "2026", "en"}, _archive_tokenize('Songkran 2026 "[EN]"')
    assert _archive_tokenize("a I 1") == set(), "single-char tokens dropped"
    assert _archive_score_title({"songkran"}, '[202604] [EN] "Songkran Festival"') >= 3, "substring hit"
    assert _archive_score_title({"songkran"}, "Loi Krathong") == 0, "no overlap"
    assert _archive_is_presence("do we have any events lined up for 2026?") is True
    assert _archive_is_presence("show me what's coming up") is True
    assert _archive_is_presence("2026") is True
    assert _archive_is_presence("Songkran") is False, "bare event name not presence"
    assert _archive_is_presence("do we have Songkran?") is False, "specific presence → title search, not list-all"
    assert _archive_content_tokens("do we have Songkran lined up?") == {"songkran"}, _archive_content_tokens("do we have Songkran lined up?")
    assert _archive_is_detail("when is Songkran?") is True
    assert _archive_is_detail("where is it held") is True
    assert _archive_is_detail("Songkran") is False, "bare event name not detail"
    print("OK archive: tokenize + title score + presence/detail classify")
