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
import base64
import json
import os
import re
import signal
import time
import unicodedata
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import html
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from .config import CONFIG
from .notebooklm import CLI, _cached_notebooks, _run_cli
from .zai import zai_message

router = APIRouter()

_WP_SECRETS = Path.home() / "n8n" / ".secrets.env"
_RAILJACK_ENV = Path.home() / ".config" / "railjack" / "env"


def _secret(key: str) -> str | None:
    """Read a secret: env first, then ~/.config/railjack/env, then ~/n8n/.secrets.env
    (KEY=value lines; values may be unquoted-with-spaces — split on first '=' + strip outer
    whitespace only, embedded spaces survive). Mirrors zai._resolve_key(); no shell-out."""
    v = os.environ.get(key)
    if v:
        return v
    for env_path in (_RAILJACK_ENV, _WP_SECRETS, Path("/home/NAZ/n8n/.secrets.env")):
        if env_path.is_file():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    return None


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
    result: dict | None = None  # SEO HEALTH report (NOT in to_dict; fetch via /seo/report/{id})

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


def _weekday_due_dates(yyyymm: str, weekdays: list[int], need: int) -> list[str]:
    """First ``need`` ISO dates (YYYY-MM-DD) whose weekday() is in ``weekdays``,
    walking forward from the 1st of ``yyyymm``. Spills into following months if
    the given month doesn't have enough matching days (short month, few
    Thursdays, etc.) so a full writer batch is always fully dated."""
    d = datetime.strptime(yyyymm + "01", "%Y%m%d")
    out: list[str] = []
    while len(out) < need:
        if d.weekday() in weekdays:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


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
    trello_json_path = Path.home() / ".config" / "railjack" / "trello.json"
    if trello_json_path.is_file():
        try:
            data = json.loads(trello_json_path.read_text())
            key = data.get("key") or data.get("api_key") or data.get("trello_key") or ""
            tok = data.get("token") or data.get("trello_token") or ""
            if key and tok:
                return str(key).strip(), str(tok).strip()
        except Exception:
            pass
    key = _secret("TRELLO_KEY") or os.environ.get("TRELLO_KEY", "")
    tok = _secret("TRELLO_TOKEN") or os.environ.get("TRELLO_TOKEN", "")
    if not key or not tok:
        raise HTTPException(
            503,
            "TRELLO_KEY/TRELLO_TOKEN not configured (set ~/.config/railjack/trello.json, env, or secrets)",
        )
    return key.strip(), tok.strip()


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
    # R4: writer desks (paul/teerin) get auto-computed due dates by weekday rule; the
    # date for #NN must follow the same cross-rerun continuation as `nn` itself, so it's
    # keyed by `cur` (the card's actual sequence number), not the loop index `i`.
    due_weekdays = desk.get("due_weekdays")
    weekday_dates = _weekday_due_dates(yyyymm, due_weekdays, nn + count - 1) if due_weekdays else None

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
        if weekday_dates is not None:
            card_params["due"] = f"{weekday_dates[cur - 1]}T00:00:00.000Z"
        else:
            if due:
                card_params["due"] = due
            if start:
                card_params["start"] = start
        card = await _trello("POST", "/cards", card_params)
        await _trello("POST", f"/cards/{card['id']}/attachments",
                      body={"url": doc_url, "name": doc_name})
        folder_url = f"https://drive.google.com/drive/folders/{desk['drive_folder_id']}"
        await _trello("POST", f"/cards/{card['id']}/attachments",
                      body={"url": folder_url, "name": "Parent folder"})
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

    # Try ISO format first (e.g., 2026-07-15 or 2026-07-15T10:00:00Z)
    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?=[T\s\D]|$)", text)
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


def _dedup_merge_events(rows, today_iso: str, window_end_iso: str, source: str = "ide") -> list[dict]:
    """Normalize + dedupe event rows, collapsing same-event duplicates (the same event
    found via multiple sources) into one row that keeps ALL their source URLs. Key =
    normalized title prefix + start_date. Used by the IDE CONVERT lane, whose handoff
    routinely lists one event across several sources. ``url`` stays the first non-empty
    (ThickBox reads it); the merged ``urls`` list carries the rest."""
    out: dict[str, dict] = {}
    for ev in rows or []:
        n = _normalize_event(ev, today_iso, window_end_iso, source)
        if not n:
            continue
        norm = re.sub(r"[^a-z0-9]+", "", (n["title"] or "").lower())[:30]
        key = f"{norm}|{n['start_date']}"
        cur = out.get(key)
        incoming = list(ev.get("urls") or []) if isinstance(ev, dict) and ev.get("urls") else []
        if cur is None:
            urls = incoming[:]
            if n["url"] and n["url"] not in urls:
                urls.insert(0, n["url"])
            n["urls"] = urls or ([n["url"]] if n["url"] else [])
            out[key] = n
            continue
        for u in (incoming or ([n["url"]] if n["url"] else [])):
            if u and u not in cur["urls"]:
                cur["urls"].append(u)
        if not cur["url"]:
            cur["url"] = n["url"]
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


# --- social lanes (agent-reach: OpenCLI) --------------------------------------
# Login-backed free backends (home-verified 2026-08-17): reddit + facebook +
# twitter via OpenCLI (rides the Chrome session through its daemon). Every lane
# no-ops to [] when the CLI/auth is missing, so the scout stays green without
# them. Ported from Somatic 912efd2; twitter lane adapted twitter-cli → opencli.

_SOCIAL_TIMEOUT = 90.0
_LOCAL_BIN = Path.home() / ".local" / "bin"


def _social_bin(name: str) -> str:
    """Prefer ~/.local/bin — systemd services don't carry the fnm PATH."""
    p = _LOCAL_BIN / name
    return str(p) if p.exists() else name


async def _social_cli(argv: list[str], timeout: float = _SOCIAL_TIMEOUT) -> str:
    """Run one social-backend CLI, returning stdout. Raises on non-zero/timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((err or out).decode(errors="replace")[:120])
    return out.decode(errors="replace")


_THAI_RE = re.compile(r"[฀-๿]")


def _is_thai(text: str) -> bool:
    return bool(_THAI_RE.search(text or ""))


def _parse_reddit_posts(text: str) -> list[dict]:
    """opencli `reddit search -f json` → scout articles (source = subreddit,
    so the 1-per-domain filter keeps several threads)."""
    try:
        rows = json.loads(text)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    arts: list[dict] = []
    for r in rows:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not title or not url:
            continue
        try:
            date = datetime.fromtimestamp(int(r.get("created_utc")), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            date = ""
        body = (r.get("selftext") or "").strip()
        sub = (r.get("subreddit") or "reddit").strip().removeprefix("r/").removeprefix("/r/")
        arts.append({"title": title[:200], "url": url,
                     "snippet": (body or title)[:300], "date": date,
                     "lang": "th" if _is_thai(title) else "en",
                     "source": f"reddit.com/r/{sub}"})
    return arts


_TW_CREATED = "%a %b %d %H:%M:%S %z %Y"


def _parse_twitter_posts(text: str, handle: str, days: int, limit: int = 6) -> list[dict]:
    """opencli `twitter tweets -f json` → scout articles (source = x.com/<handle>),
    recency-filtered to ``days``. Short posts are dropped."""
    try:
        data = json.loads(text)
        rows = data.get("data") if isinstance(data, dict) else data
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    arts: list[dict] = []
    cutoff = datetime.now() - timedelta(days=days)
    for r in rows:
        created_raw = (r.get("created_at") or r.get("createdAt") or "").strip()
        try:
            created = datetime.strptime(created_raw, _TW_CREATED).replace(tzinfo=None)
        except ValueError:
            continue
        if created < cutoff:
            continue
        txt = re.sub(r"\s+", " ", (r.get("text") or "").strip())
        if len(txt) < 40:
            continue
        author = r.get("author")
        screen = ((author.get("screenName") if isinstance(author, dict) else author) or handle).strip()
        arts.append({"title": txt[:120],
                     "url": f"https://x.com/{screen}/status/{r.get('id', '')}",
                     "snippet": txt[:300], "date": created.strftime("%Y-%m-%d"),
                     "lang": "th" if _is_thai(txt) else "en",
                     "source": f"x.com/{screen}"})
        if len(arts) >= limit:
            break
    return arts


# FB style: "FRI, AUG 28 AT 11 AM" — all-caps month, optional comma, no year
_FB_DATE = re.compile(r"\b([A-Za-z]{3})[ ,]+(\d{1,2}) AT \d{1,2} (?:AM|PM)\b")


def _fb_year_resolve(month: int, day: int, today: datetime, window_end_iso: str) -> str | None:
    """FB event text carries no year — pick the occurrence inside the window."""
    for year in (today.year, today.year + 1):
        try:
            iso = datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None
        if today.strftime("%Y-%m-%d") <= iso <= window_end_iso:
            return iso
    return None


def _parse_fb_events(text: str, today_iso: str, window_end_iso: str) -> list[dict]:
    """opencli `facebook search -f json` rows with title=='Events' →
    _normalize_event input dicts. Bold-unicode text is NFKC-normalized; the
    date marker (e.g. 'FRI, AUG 28 AT 11 AM AND 2 MORE') resolves the year."""
    try:
        rows = json.loads(text)
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    today = datetime.strptime(today_iso, "%Y-%m-%d")
    out: list[dict] = []
    for r in rows:
        if (r.get("title") or "").strip() != "Events":
            continue
        raw = unicodedata.normalize("NFKC", r.get("text") or "")
        m = _FB_DATE.search(raw)
        if not m:
            continue
        month = MONTHS_MAP.get(m.group(1)[:3].lower())
        start = _fb_year_resolve(month, int(m.group(2)), today, window_end_iso) if month else None
        if not start:
            continue
        tail = re.sub(r"^\s*(?:AND \d+ MORE\s*)+", "", raw[m.end():])
        name = re.split(r"\s·\s|\s?\d+ (?:people|going)", tail, 1)[0].strip(" ·–-")
        out.append({"title": (name or "Facebook event")[:200],
                    "url": (r.get("url") or "").strip(),
                    "start_date": start,
                    "summary": raw[:300]})
    return out


async def _social_news_lanes(main_query: str, days: int) -> tuple[list[dict], list[str]]:
    """Reddit + Twitter audience-signal for the story scout. Lane errors are
    soft — appended to the scout's error list, never fatal."""
    arts: list[dict] = []
    errs: list[str] = []
    reddit_q = main_query.replace(" news", "").strip()
    if reddit_q:
        try:
            out = await _social_cli([_social_bin("opencli"), "reddit", "search", reddit_q,
                                     "--limit", "8", "-f", "json"])
            arts.extend(_parse_reddit_posts(out))
        except Exception as e:
            errs.append(f"reddit: {e}")
    # anchor accounts verified live 2026-08-17 (home: opencli twitter tweets) —
    # override via options.social_twitter_accounts
    for h in (_opts().get("social_twitter_accounts")
              or ["ThaiPBSWorld", "BangkokPostNews", "Thairath_News"])[:3]:
        try:
            out = await _social_cli([_social_bin("opencli"), "twitter", "tweets", f"@{h}",
                                     "--limit", "15", "-f", "json"])
            arts.extend(_parse_twitter_posts(out, h, days))
        except Exception as e:
            errs.append(f"twitter @{h}: {e}")
    return arts, errs


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

    # social lane (agent-reach/OpenCLI): Facebook events — Jina can't reach FB
    # (login-walled); OpenCLI rides the Chrome session. Lane no-ops without it.
    fb_events: list[dict] = []
    try:
        fb_q = (f"Thailand {category} event".replace("  ", " ").strip()
                if category else queries[0])
        fb_out = await _social_cli([_social_bin("opencli"), "facebook", "search", fb_q,
                                    "--limit", "10", "-f", "json"])
        fb_events = [ev for ev in _parse_fb_events(fb_out, today_iso, window_end_iso)
                     if ev.get("title") and ev.get("url")]
    except Exception as e:
        errors.append(f"facebook: {e}")

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
    for ev in fb_events:
        n = _normalize_event(ev, today_iso, window_end_iso, "facebook")
        if n:
            events.setdefault(n["url"] or n["title"], n)
    ordered = sorted(events.values(), key=lambda e: e.get("start_date") or "9999")
    return {"events": ordered, "count": len(ordered), "errors": errors,
            "window": {"from": today_iso, "to": window_end_iso, "weeks": weeks}}


# Antigravity IDE scout writes its handoff here (see 10-knowledge/thailandnow-events-
# antigravity-handoff.md). Module-level so tests can monkeypatch it off real /tmp.
_IDE_HANDOFF = Path("/tmp/thailand-now-events/latest.json")


@router.post("/api/thailandnow/events/convert")
async def convert_ide_events(payload: dict = Body(default={})):
    """IDE CONVERT — read the Antigravity-scout handoff (``_IDE_HANDOFF`` JSON) and return
    Tier-1-shaped events: window-filtered, multi-source dupes merged (all URLs kept),
    sorted by start_date. SAME shape as ``scout_events`` so the frontend merges identically.
    Keyless, no LLM. Soft-fails to HTTP 200 ``{events:[], count:0, errors:[...]}`` when the
    handoff is missing/unparseable — points the user at 📋 IDE SCOUT first."""
    body = payload or {}
    weeks = max(1, min(52, int(body.get("weeks") or 4)))
    today = datetime.now()
    window_end = today + timedelta(weeks=weeks)
    today_iso, window_end_iso = today.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")

    missing = {"events": [], "count": 0, "errors": ["no IDE handoff file — run 📋 IDE SCOUT first"]}
    if not _IDE_HANDOFF.exists():
        return missing
    try:
        data = json.loads(_IDE_HANDOFF.read_text(encoding="utf-8"))
        rows = data.get("events") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not isinstance(rows, list):
            rows = []
    except Exception:
        return missing

    merged = _dedup_merge_events(rows, today_iso, window_end_iso, source="ide")
    ordered = sorted(merged, key=lambda e: e.get("start_date") or "9999")
    return {"events": ordered, "count": len(ordered), "errors": []}


# --- STORY SCOUT (news pitch discovery + make-a-pitch) ---

# Domains never surfaced as pitch candidates (the outlet's own site + known noise).
_SCOUT_EXCLUDE_DOMAINS = {"thailandnow.in.th"}


def _scout_domain_excluded(domain: str) -> bool:
    """True if the domain is the outlet's own site (or a subdomain of it)."""
    return any(domain == ex or domain.endswith("." + ex) for ex in _SCOUT_EXCLUDE_DOMAINS)


# --- Negative/controversial FRAMING screen (home port of Tasai's Somatic be3fb78,
# 2026-08-21 — record: 20-projects/thailand-now-story-scout.md). Deterministic
# belt-and-braces UNDER the gem rules: drops stories whose title/snippet is framed
# as a trap / myth-bust / warning / crackdown / skip-avoid piece. The audience
# wants constructive/neutral framing; fear-framing is Ben's lane, not ours.
_NEGATIVE_FRAMING_TERMS = (
    # EN — trap/myth/warning angles
    "tourist trap", "expat trap", "traps in thailand", "traps to avoid",
    "myths about", "myth of", "debunk", "warning for", "you've been warned",
    # crackdown stories
    "crackdown on", "crackdown targets", "police sweep", "war on tourists",
    # skip/avoid listicles
    "avoid in thailand", "avoid these", "places to avoid", "things to avoid",
    "mistakes to avoid", "never do in", "skip these", "don't do in", "do not do in",
    "scam alert", "scams to avoid",
    # TH — same framing families
    "กับดักนักท่องเที่ยว", "กับดักชาวต่างชาติ", "เมธีเท็จ", "เตือนภัย", "ระวังกับดัก",
    "อย่าไป", "อย่าทำ", "เลี่ยงที่", "ที่ควรเลี่ยง", "สถานที่ที่ควรหลีกเลี่ยง",
    "จับกุมนักท่องเที่ยว", "ปราบนักท่องเที่ยว", "มิจฉาชีพหลอก",
)


def _is_negative_framing(title: str, snippet: str = "") -> bool:
    """True when title+snippet text matches a negative-framing denylist term.
    Substring match on the lowercased concatenation — Thai terms are case-less."""
    hay = f"{title} {snippet}".lower()
    return any(t in hay for t in _NEGATIVE_FRAMING_TERMS)


def _screen_negative(items: list, title_key: str = "title", snippet_key: str = "snippet") -> tuple[list, int]:
    """Deterministic negative-framing filter. Returns (kept, dropped_count) —
    never mutates the input list."""
    kept: list = []
    dropped = 0
    for it in items:
        if _is_negative_framing(str(it.get(title_key) or ""), str(it.get(snippet_key) or "")):
            dropped += 1
        else:
            kept.append(it)
    return kept, dropped


def _scout_date_in_range(date_str: str, cutoff_iso: str, today_iso: str) -> bool:
    """True only for a parsed date within [cutoff, today]. Strict policy: undated
    (empty) is False → dropped. Recency is guaranteed; the list is short because
    Jina exposes no date for ~80% of pages — widen by improving date-capture, not
    by loosening this filter."""
    return bool(date_str) and cutoff_iso <= date_str <= today_iso


def _looks_like_url(s: str | None) -> bool:
    return bool(s) and bool(re.match(r"^https?://\S+$", s.strip()))


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


def _resolve_gem(opt_key: str, default: str) -> Path:
    """Resolve a gem path from options; relative paths anchor at the repo root.
    Shared by the publicity/archive/scout gem-path resolvers."""
    p = Path(_opts().get(opt_key, default))
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def _scout_gem_path() -> Path:
    """STORY SCOUT pitch gem path."""
    return _resolve_gem("scout_gem_path", "app/gems/story-scout-pitch.md")


_JINA_META = ("title:", "url source:", "markdown content:", "published time:", "description:")
_BOTCHECK = ("just a moment", "security checkpoint", "attention required",
             "vercel security", "enable javascript", "checking your browser",
             "forbidden", "warning: target url")


def _extract_title(lines: list[str], non_blank: list[str]) -> str:
    """Clean an article title from Jina-split lines: first #-heading (fallback
    first non-blank), strip Jina metadata prefix + markdown link/image syntax,
    drop bot-check/error pages (→ ""). Shared by _extract_news and scout_pitch."""
    title = ""
    for l in lines:
        if l.startswith("#"):
            title = l.lstrip("#").strip()
            if title:
                break
    if not title and non_blank:
        title = non_blank[0]
    if title.lower().startswith(_JINA_META):
        title = title.split(":", 1)[1].strip()
    if any(b in title.lower() for b in _BOTCHECK):
        return ""
    title = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", title)      # ![alt](url) → drop
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)  # [text](url) → text
    return title.strip(" #")[:200]


def _extract_news(md: str, url: str) -> dict | None:
    """Walk Jina markdown of ONE news article, pulling news fields."""
    if not md:
        return None
    lines = [line.strip() for line in md.split("\n")]
    non_blank = [l for l in lines if l]
    if not non_blank:
        return None

    title = _extract_title(lines, non_blank)
    if not title:
        return None

    snippet = ""
    for l in lines:
        if not l:
            continue
        if l.startswith("#") or l.startswith(">"):
            continue
        if l.startswith("![") or ("![" in l and "](" in l):
            continue
        if l.lower().startswith(_JINA_META):
            continue
        if l == title or l == f"# {title}":
            continue
        snippet = l[:300]
        break

    date = ""
    for l in lines:  # Jina's "Published Time: <ISO>" is the reliable publish date
        if l.lower().startswith("published time:"):
            d = _parse_date(l.split(":", 1)[1])
            if d:
                date = d
                break
    if not date:  # fallback: regex-scan the top of the content
        for l in lines[:20]:
            d = _parse_date(l)
            if d:
                date = d
                break

    lang = "th" if re.search(r"[ก-๙]", title + " " + snippet) else "en"

    try:
        host = urllib.parse.urlparse(url).hostname or ""
        source = host.removeprefix("www.")
    except Exception:
        source = url

    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "date": date,
        "lang": lang,
        "source": source,
    }


async def _scout_news(query: str | None = None, category: str | None = None, days: int = 7, exact: bool = False) -> dict:
    """News-pitch search (PITCH mode discovery). Free-first multi-source sweep
    (DDG + Brave + GNews + Jina read + regex extraction)."""
    days = max(1, min(30, int(days or 7)))
    if _looks_like_url(query):
        return await _scout_lookup_url((query or "").strip())
    if exact and (query or "").strip():
        return await _scout_lookup_title((query or "").strip(), days)
    today = datetime.now()
    span = today.strftime("%B %Y")
    cutoff_dt = today - timedelta(days=days)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%d")
    today_iso = today.strftime("%Y-%m-%d")

    cat_clauses = {
        "expat-policy": "visa OR immigration OR work permit OR regulation",
        "business-investment": "business OR investment OR economy OR property",
        "lifestyle": "lifestyle OR cost of living OR travel OR healthcare",
    }
    cat_clause = cat_clauses.get((category or "").strip(), "")
    cat_clause_thai = {
        "expat-policy": "วีซ่า OR ตรวจคนเข้าเมือง OR ใบอนุญาตทำงาน OR กฎระเบียบ",
        "business-investment": "ธุรกิจ OR การลงทุน OR เศรษฐกิจ OR อสังหาริมทรัพย์",
        "lifestyle": "การดำเนินชีวิต OR ค่าครองชีพ OR การท่องเที่ยว OR การดูแลสุขภาพ",
    }.get((category or "").strip(), "")

    q_clean = (query or "").strip()
    if q_clean:
        queries = [
            f"{q_clean} {span}",
            f"Thailand {q_clean} news {span}",
            f"ประเทศไทย {q_clean} {span}",
            f"{q_clean} site:thairath.co.th OR site:khaosod.co.th OR site:matichon.co.th OR site:prachachat.net",
        ]
    else:
        c_en = f" {cat_clause}" if cat_clause else ""
        c_th = f" {cat_clause_thai}" if cat_clause_thai else ""
        queries = [
            f"Thailand{c_en} {span}".strip(),
            f"Thailand{c_en} news {span}".strip(),
            f"ประเทศไทย{c_th} {span}".strip(),
            f"Thailand{c_en} site:thairath.co.th OR site:khaosod.co.th OR site:matichon.co.th OR site:prachachat.net".strip(),
        ]

    errors: list[str] = []
    ddg_urls: list[str] = []
    for q in queries:
        try:
            md = await _jina_read(f"https://duckduckgo.com/html/?q={urllib.parse.quote(q)}")
            for ev in _parse_ddg(md):
                ddg_urls.append(ev["url"])
        except Exception as e:
            errors.append(f"ddg {q!r}: {e}")

    brave_results = await asyncio.gather(*[_brave_urls(q) for q in queries])
    brave_urls = [u for batch in brave_results for u in batch]

    gnews_q = f"{q_clean} news" if q_clean else (f"Thailand {cat_clause} news".strip() if cat_clause else f"Thailand news {span}")
    gnews_urls = await _gnews_urls(gnews_q)

    urls = _scout_dedup([*ddg_urls, *brave_urls, *gnews_urls], by_domain=True, limit=20)

    async def _fetch_and_extract(u: str) -> tuple[dict | None, str | None]:
        try:
            md = await _jina_read(u)
            return _extract_news(md, u), None
        except Exception as e:
            return None, f"extract {u}: {e}"

    extract_results = await asyncio.gather(*[_fetch_and_extract(u) for u in urls])
    ordered: list[dict] = []
    for res, err in extract_results:
        if err:
            errors.append(err)
        elif res:
            d_str = res.get("date") or ""
            if not _scout_date_in_range(d_str, cutoff_iso, today_iso):
                continue  # strict: undated or out-of-window dropped
            ordered.append(res)

    # social lanes (agent-reach/OpenCLI): reddit threads + twitter anchor
    # accounts — audience signal the wires can't give; pre-fetched, no Jina.
    social_arts, social_errs = await _social_news_lanes(query or "Thailand news", days)
    errors.extend(social_errs)
    ordered.extend(a for a in social_arts
                   if _scout_date_in_range(a.get("date") or "", cutoff_iso, today_iso))

    ordered = await _scout_rerank(ordered)
    ordered, neg_dropped = _screen_negative(ordered)

    return {
        "results": ordered,
        "count": len(ordered),
        "negative_dropped": neg_dropped,
        "errors": errors,
        "query": query,
        "category": category,
        "days": days,
    }


def _scout_apply_rerank(candidates: list[dict], llm_output: list[dict] | None) -> list[dict]:
    """Pure helper to apply LLM rerank output to candidates list.
    llm_output is [{idx, score, keep}]. Drops keep:false, sorts keep:true by score desc,
    appends unmapped candidates in original order."""
    if not candidates or not isinstance(llm_output, list):
        return candidates

    rank_map: dict[int, dict] = {}
    for item in llm_output:
        if isinstance(item, dict) and "idx" in item:
            try:
                idx = int(item["idx"])
                rank_map[idx] = item
            except (ValueError, TypeError):
                continue

    kept_scored: list[tuple[float, int, dict]] = []
    unmapped: list[dict] = []

    for i, c in enumerate(candidates):
        if i in rank_map:
            info = rank_map[i]
            keep = info.get("keep", True)
            if keep:
                try:
                    score = float(info.get("score", 0))
                except (ValueError, TypeError):
                    score = 0.0
                kept_scored.append((score, i, c))
        else:
            unmapped.append(c)

    kept_scored.sort(key=lambda x: (-x[0], x[1]))
    sorted_kept = [c for _, _, c in kept_scored]
    return sorted_kept + unmapped


async def _scout_rerank(candidates: list[dict]) -> list[dict]:
    """LLM editorial rerank for foreigner-in-Thailand audience.
    Degrades gracefully to unranked candidates on gateway failure / invalid output."""
    if not candidates:
        return candidates
    try:
        top_candidates = candidates[:15]
        prompt_items = [
            {"idx": i, "title": c.get("title", ""), "snippet": c.get("snippet", "")}
            for i, c in enumerate(top_candidates)
        ]
        prompt = json.dumps(prompt_items, ensure_ascii=False)
        system = _load_gem(_resolve_gem("scout_rerank_gem_path", "app/gems/story-scout-rerank.md"))
        opts = _opts()
        model = (opts.get("scout_llm") or {}).get("model") or "glm-5"
        llm_out = await _llm_json(prompt, system=system, model=model)
        if isinstance(llm_out, list):
            return _scout_apply_rerank(candidates, llm_out)
    except Exception:
        pass
    return candidates


async def _pexels_photos(query: str, per_page: int = 8) -> list[dict]:
    """Fetch stock photos from Pexels API (requires PEXELS_API_KEY). Uses real Chrome UA to bypass Cloudflare."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key or not query:
        return []
    headers = {
        "Authorization": key,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    params = {"query": query, "per_page": per_page, "orientation": "landscape"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.pexels.com/v1/search", headers=headers, params=params)
            if r.status_code != 200:
                return []
            data = r.json()
            photos = data.get("photos", [])
            out: list[dict] = []
            for p in photos:
                if isinstance(p, dict) and p.get("src"):
                    src = p["src"]
                    out.append({
                        "url": src.get("original", ""),
                        "thumb": src.get("medium", ""),
                        "w": p.get("width", 0),
                        "h": p.get("height", 0),
                        "provider": "pexels",
                    })
            return out
    except Exception:
        return []


async def _pixabay_photos(query: str, per_page: int = 8) -> list[dict]:
    """Fetch stock photos from Pixabay API (requires PIXABAY_API_KEY)."""
    key = os.environ.get("PIXABAY_API_KEY")
    if not key or not query:
        return []
    params = {
        "key": key,
        "q": query,
        "per_page": per_page,
        "image_type": "photo",
        "orientation": "horizontal",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://pixabay.com/api/", params=params)
            if r.status_code != 200:
                return []
            data = r.json()
            hits = data.get("hits", [])
            out: list[dict] = []
            for h in hits:
                if isinstance(h, dict):
                    out.append({
                        "url": h.get("largeImageURL", ""),
                        "thumb": h.get("webformatURL", ""),
                        "w": h.get("imageWidth", 0),
                        "h": h.get("imageHeight", 0),
                        "provider": "pixabay",
                    })
            return out
    except Exception:
        return []


def _scout_filter_stock(photos: list[dict]) -> list[dict]:
    """Filter stock photos to h >= 1080, dedupe by url, and sort by (h * w) descending."""
    seen: set[str] = set()
    filtered: list[dict] = []
    for p in photos:
        h = p.get("h", 0)
        u = p.get("url", "")
        if h >= 1080 and u and u not in seen:
            seen.add(u)
            filtered.append(p)
    filtered.sort(key=lambda p: p.get("h", 0) * p.get("w", 0), reverse=True)
    return filtered


async def _scout_images_content(url: str) -> dict:
    """Gather images for a news story: Tier 1 (article images) -> Tier 2 (stock >= 1080p) -> Tier 3 (AI prompts)."""
    md = await _jina_read(url)
    tier1_raw = _parse_images(md)
    tier1 = [{"url": img.get("url", ""), "alt": img.get("alt", ""), "tier": 1} for img in tier1_raw]

    lines = [l.strip() for l in md.split("\n")]
    non_blank = [l for l in lines if l]
    title = _extract_title(lines, non_blank)

    digest_prompt = f"Title: {title}\nURL: {url}\n\nContent:\n{md[:6000]}"
    system = _load_gem(_resolve_gem("scout_image_digest_gem_path", "app/gems/story-scout-image-digest.md"))
    opts = _opts()
    model = (opts.get("scout_llm") or {}).get("model") or "glm-5"

    try:
        digest_data = await _llm_json(digest_prompt, system=system, model=model)
    except Exception:
        digest_data = {}

    if not isinstance(digest_data, dict):
        digest_data = {}

    stock_queries = digest_data.get("stock_queries", [])
    if not isinstance(stock_queries, list):
        stock_queries = []
    ai_prompts = digest_data.get("ai_prompts", [])
    if not isinstance(ai_prompts, list):
        ai_prompts = []

    stock_tasks = []
    for q in stock_queries[:5]:
        if isinstance(q, str) and q.strip():
            stock_tasks.append(_pexels_photos(q.strip()))
            stock_tasks.append(_pixabay_photos(q.strip()))

    tier2_raw: list[dict] = []
    if stock_tasks:
        stock_results = await asyncio.gather(*stock_tasks)
        for batch in stock_results:
            tier2_raw.extend(batch)

    tier2_filtered = _scout_filter_stock(tier2_raw)[:12]
    tier2 = [{**img, "tier": 2} for img in tier2_filtered]

    return {
        "tier1": tier1,
        "tier2": tier2,
        "ai_prompts": [str(p) for p in ai_prompts if p],
        "url": url,
    }


async def _flow_scout_search(job: TnJob, query: str | None, category: str | None, days: int, exact: bool) -> None:
    job.result = await _scout_news(query=query, category=category, days=days, exact=exact)


@router.post("/api/thailandnow/scout/search")
async def scout_search(payload: dict = Body(default={})):
    """STORY SCOUT — news pitch search route (async job)."""
    body = payload or {}
    query = body.get("query")
    category = body.get("category")
    days = int(body.get("days") or 7)
    exact = bool(body.get("exact", False))
    if any(j.kind == "scout-search" and j.status in _TN_RUNNING for j in _TN_JOBS.values()):
        raise HTTPException(409, "a STORY SCOUT search is already running")
    label = f"scout: {(query or category or 'general')[:40]}"
    return _tn_spawn("scout-search", label,
                     lambda j: _flow_scout_search(j, query, category, days, exact))


@router.get("/api/thailandnow/scout/report/{jid}")
async def scout_report(jid: str):
    """Fetch results of a completed STORY SCOUT search job."""
    job = _TN_JOBS.get(jid)
    if not job or job.kind != "scout-search":
        raise HTTPException(404, "no such STORY SCOUT job")
    if job.status != "done":
        raise HTTPException(409, f"job is {job.status}; not ready")
    return job.result


@router.post("/api/thailandnow/scout/pitch")
async def scout_pitch(payload: dict = Body(default={})):
    """STORY SCOUT — make a pitch for a news article URL."""
    body = payload or {}
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")

    md = await _jina_read(url)
    lines = [l.strip() for l in md.split("\n")]
    non_blank = [l for l in lines if l]
    title = _extract_title(lines, non_blank)

    system = _load_gem(_scout_gem_path())
    opts = _opts()
    model = (opts.get("scout_llm") or {}).get("model") or "glm-5"
    user = f"Title: {title}\nSource URL: {url}\n\nArticle:\n{md[:20000]}"

    try:
        raw = await zai_message(user, max_tokens=8192, system=system, model=model, timeout=180)
        pitch = _parse_json_lenient(raw) or {"headline_en": raw.strip()}
        mode = "direct"
    except (HTTPException, Exception):
        pitch, mode = {}, "degraded"

    return {"pitch": pitch, "url": url, "model": model, "mode": mode}


@router.post("/api/thailandnow/scout/images")
async def scout_images(payload: dict = Body(default={})):
    """STORY SCOUT — gather multi-tier images for a news story URL."""
    body = payload or {}
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    try:
        return await _scout_images_content(url)
    except (HTTPException, Exception) as e:
        return {"tier1": [], "tier2": [], "ai_prompts": [], "url": url, "error": str(e)}


# IDE-lane image handoff (📋 IDE IMAGES → Antigravity writes, ⇄ CONVERT reads).
_SCOUT_IMAGES_HANDOFF = Path("/tmp/railjack-images/latest.json")


@router.get("/api/thailandnow/scout/images/convert")
async def scout_images_convert():
    """IDE CONVERT — read the image handoff (``_SCOUT_IMAGES_HANDOFF``) and return
    the SAME shape as ``scout_images`` so the frontend renders it identically.
    Keyless, no LLM, no stock APIs. Soft-fails to HTTP 200 with ``error`` set."""
    p = _SCOUT_IMAGES_HANDOFF
    if not p.exists():
        return {"tier1": [], "tier2": [], "ai_prompts": [], "url": "",
                "error": "no IDE image handoff — run 📋 IDE IMAGES first"}

    def _imgs(v) -> list[dict]:
        rows = v if isinstance(v, list) else []
        out = []
        for it in rows:
            if isinstance(it, str):
                it = {"url": it}
            if not isinstance(it, dict):
                continue
            u = str(it.get("url") or "").strip()
            if u:
                out.append({"url": u, "alt": str(it.get("alt") or "").strip()})
        return out

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"tier1": [], "tier2": [], "ai_prompts": [], "url": "",
                "error": f"handoff isn't valid JSON: {e}"}
    if not isinstance(data, dict):
        return {"tier1": [], "tier2": [], "ai_prompts": [], "url": "",
                "error": "handoff must be a JSON object"}
    prompts = data.get("ai_prompts")
    if not isinstance(prompts, list):
        prompts = [str(prompts)] if prompts else []
    tier1, tier2 = _imgs(data.get("tier1")), _imgs(data.get("tier2"))
    # dedup urls across tiers (tier1 wins)
    seen = {im["url"] for im in tier1}
    tier2_dedup: list[dict] = []
    for im in tier2:
        if im["url"] not in seen:
            seen.add(im["url"])
            tier2_dedup.append(im)
    tier2 = tier2_dedup
    return {
        "tier1": [{**im, "tier": 1} for im in tier1],
        "tier2": [{**im, "tier": 2} for im in tier2],
        "ai_prompts": [str(x) for x in prompts if str(x).strip()],
        "url": str(data.get("url") or "").strip(),
    }


@router.post("/api/thailandnow/scout/wp-media")
async def scout_wp_media(payload: dict = Body(default={})):
    """SEND TO WP — upload one image to the Media Library with metadata.
    Fields arrive already-final (frontend is editable), so we upload them verbatim."""
    body = payload or {}
    image_url = (body.get("image_url") or "").strip()
    if not image_url:
        raise HTTPException(400, "image_url required")
    return await _wp_upload_media(
        image_url=image_url,
        title=(body.get("title") or "").strip(),
        alt_text=(body.get("alt_text") or "").strip(),
        caption=(body.get("caption") or "").strip(),
    )


_SCOUT_HANDOFF = Path("/tmp/railjack-scout/latest.json")


@router.get("/api/thailandnow/scout/terminal-report")
async def scout_terminal_report():
    """CONVERT — read the JSON the /story-scout skill wrote to disk. Returns
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
            seen.add(r["url"])
            deduped.append(r)
    deduped, neg_dropped = _screen_negative(deduped)
    return {"results": deduped, "count": len(deduped), "negative_dropped": neg_dropped,
            "mtime": p.stat().st_mtime}


# --- FIRESIDE MODE (Slice 1): Topic Sourcing & Script Edit Notes ---

_FIRESIDE_NB_PREFIX = "The Fireside"


def _fireside_nb_id_path() -> Path:
    """Sidecar holding the dedicated Fireside notebook id."""
    return Path(os.path.expanduser("~/.config/railjack/fireside_notebook.id"))


async def _fireside_nid() -> str | None:
    """3-layer lookup for the Fireside notebook ID, first hit wins:
    1. options: opts.get("fireside_notebook_id")
    2. sidecar: read _fireside_nb_id_path() if present
    3. discover: scan _cached_notebooks() for title startswith _FIRESIDE_NB_PREFIX
    """
    opts = _opts()
    opt_nid = opts.get("fireside_notebook_id")
    if opt_nid:
        return str(opt_nid).strip()

    p = _fireside_nb_id_path()
    if p.is_file():
        try:
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception:
            pass

    try:
        nbs = await _cached_notebooks()
        for nb in nbs:
            title = str(nb.get("title") or "")
            if title.startswith(_FIRESIDE_NB_PREFIX):
                nid = nb.get("id")
                if nid:
                    return str(nid).strip()
    except Exception:
        pass
    return None


async def _fireside_ensure() -> str:
    """Discover the Fireside corpus notebook. Discover only — never create."""
    nid = await _fireside_nid()
    if not nid:
        raise HTTPException(
            424,
            "Create a notebook titled 'The Fireside…' in the RESEARCH tab, add the episode sources, "
            "wait for READY, then retry — or set fireside_notebook_id in options.",
        )
    return nid


def _filter_fireside_registry(rows: list[dict]) -> tuple[list[str], list[dict]]:
    """Split registry rows into (done_or_excluded_topic_titles, revisitable_candidates).
    Drops 'done' and 'excluded' from candidates while collecting their topic names for exclusion.
    Keeps 'revisitable' rows as candidates.
    """
    done_topics: list[str] = []
    revisitable: list[dict] = []
    for r in rows:
        topic = (r.get("topic") or "").strip()
        status = (r.get("status") or "").strip().lower()
        if status in ("done", "excluded"):
            if topic:
                done_topics.append(topic)
        elif status == "revisitable":
            revisitable.append(r)
    return done_topics, revisitable


async def _fireside_registry() -> list[dict]:
    """Read the Fireside Topic Registry Google Sheet (tab 'Topics').
    Returns a list of dicts: [{video_id, run, ep, topic, status, co_host, upload_date, angle_notes}, ...].
    """
    sheet_id = _opts().get("fireside_registry_sheet_id") or "1JG7xFiCmMgPx4APFB2U9tRj56yVP5Abz36t0bi0BgWs"
    token = await _google_token()
    hdr = {"Authorization": f"Bearer {token}"}
    range_param = urllib.parse.quote("Topics!A:Z")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range_param}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=hdr)
        if r.status_code != 200:
            raise HTTPException(502, f"Google Sheets read failed: {r.text[:200]}")
        data = r.json()
        values = data.get("values", [])
        if not values or len(values) < 2:
            return []

        def _normalize_header(h: str) -> str:
            clean = re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")
            mapping = {
                "videoid": "video_id",
                "video_id": "video_id",
                "run": "run",
                "ep": "ep",
                "topic": "topic",
                "status": "status",
                "co_host": "co_host",
                "cohost": "co_host",
                "uploaddate": "upload_date",
                "upload_date": "upload_date",
                "angle_notes": "angle_notes",
                "angle": "angle_notes",
                "notes": "angle_notes",
            }
            return mapping.get(clean, clean or "col")

        headers = [_normalize_header(h) for h in values[0]]
        rows: list[dict] = []
        for raw_row in values[1:]:
            if not raw_row or not any(raw_row):
                continue
            item = {}
            for i, col in enumerate(headers):
                item[col] = str(raw_row[i]).strip() if i < len(raw_row) else ""
            rows.append(item)
        return rows


# --- Covered-events dedup (double filter: OURS published + COMPANY plan) ---
# OURS sheet: Event Title in col A (row 1 = header).
# COMPANY sheet: Event Name in col C (row 2 = header), format "[YYYYMM] [EN] Name".
COVERED_OURS_SHEET = "1Hk3o7eui5S_fvC7ptZWZceT3PBIT9SUQPf_iMkddXoI"
COVERED_COMPANY_SHEET = "1LO32cJTCSN0XEUPiuEjQmeWr0LU-ohY7ca1GWQv2-N8"
_COVERED_COMPANY_PREFIX = re.compile(r"^\s*\[\d{6}\]\s*\[[^\]]*\]\s*")


def _covered_slug(title: str) -> str:
    """Unicode-aware slug for fuzzy covered-match (keeps Thai letters + digits)."""
    return re.sub(r"[^\w]", "", (title or "").lower(), flags=re.UNICODE)


async def _read_sheet_col(sheet_id: str, range_param: str, col: int = 0, header_rows: int = 1) -> list[str]:
    """Read one column of a Google Sheet range as plain strings (skips header_rows)."""
    token = await _google_token()
    hdr = {"Authorization": f"Bearer {token}"}
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(range_param)}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=hdr)
        if r.status_code != 200:
            raise HTTPException(502, f"Sheets read failed ({sheet_id}): {r.text[:160]}")
        values = r.json().get("values", [])
    out: list[str] = []
    for row in values[header_rows:]:
        cell = (row[col] if col < len(row) else "").strip()
        if cell:
            out.append(cell)
    return out


async def _sheet_append_rows(sheet_id: str, tab: str, rows: list[list[str]]) -> None:
    """Append rows under existing data of tab in Google Sheet."""
    token = await _google_token()
    hdr = {"Authorization": f"Bearer {token}"}
    rng = f"{tab}!A1" if tab else "A1"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(rng)}:append?valueInputOption=RAW"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=hdr, json={"values": rows})
        if r.status_code != 200:
            raise HTTPException(502, f"Sheets append failed ({sheet_id}): {r.text[:160]}")


async def _sheet_update_range(sheet_id: str, tab: str, range_param: str, rows: list[list[str]]) -> None:
    """Update a specific range in Google Sheet with rows."""
    token = await _google_token()
    hdr = {"Authorization": f"Bearer {token}"}
    full_range = f"{tab}!{range_param}" if tab else range_param
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(full_range)}?valueInputOption=RAW"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(url, headers=hdr, json={"values": rows})
        if r.status_code != 200:
            raise HTTPException(502, f"Sheets update failed ({sheet_id}): {r.text[:160]}")


async def _sheet_update_cell(sheet_id: str, tab: str, row_number: int, col_letter: str, value: str) -> None:
    """Update a single cell in Google Sheet."""
    await _sheet_update_range(sheet_id, tab, f"{col_letter}{row_number}", [[value]])


async def _sheet_read_all(sheet_id: str, tab: str) -> list[list[str]]:
    """Read all rows (A1:F10000) from tab as raw strings."""
    token = await _google_token()
    hdr = {"Authorization": f"Bearer {token}"}
    rng = f"{tab}!A1:F10000" if tab else "A1:F10000"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(rng)}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=hdr)
        if r.status_code != 200:
            raise HTTPException(502, f"Sheets read failed ({sheet_id}): {r.text[:160]}")
        return r.json().get("values", [])


async def _ensure_pipeline_tab() -> None:
    """Ensure the 'Pipeline' tab exists in the OURS Covered Events Registry sheet."""
    token = await _google_token()
    hdr = {"Authorization": f"Bearer {token}"}
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{COVERED_OURS_SHEET}?fields=sheets.properties.title"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=hdr)
        if r.status_code != 200:
            raise HTTPException(502, f"Sheets metadata read failed ({COVERED_OURS_SHEET}): {r.text[:160]}")
        sheets = r.json().get("sheets", [])
        titles = [s.get("properties", {}).get("title") for s in sheets if isinstance(s, dict)]
        if "Pipeline" not in titles:
            batch_url = f"https://sheets.googleapis.com/v4/spreadsheets/{COVERED_OURS_SHEET}:batchUpdate"
            batch_body = {"requests": [{"addSheet": {"properties": {"title": "Pipeline"}}}]}
            r_add = await c.post(batch_url, headers=hdr, json=batch_body)
            if r_add.status_code != 200:
                raise HTTPException(502, f"Sheets addSheet failed ({COVERED_OURS_SHEET}): {r_add.text[:160]}")
            header = ["Provisioned At", "Event Title", "Slug", "Trello Card", "Doc Link", "Status"]
            await _sheet_append_rows(COVERED_OURS_SHEET, "Pipeline", [header])


async def _pipeline_find_row(slug: str) -> int | None:
    """Find 1-based row number in Pipeline tab matching slug; None if absent."""
    await _ensure_pipeline_tab()
    rows = await _sheet_read_all(COVERED_OURS_SHEET, "Pipeline")
    target_slug = _covered_slug(slug)
    if not target_slug:
        return None
    for idx, row in enumerate(rows, start=1):
        if idx == 1:
            continue
        col_slug = row[2] if len(row) > 2 else ""
        if _covered_slug(col_slug) == target_slug:
            return idx
    return None


async def _covered_events() -> tuple[dict[str, str], list[str]]:
    """Return ({slug: source}, errors). A title is 'covered' if its slug is in
    EITHER sheet (double filter) or in pipeline. One unreadable sheet doesn't blank the set."""
    out: dict[str, str] = {}
    errors: list[str] = []
    try:
        for t in await _read_sheet_col(COVERED_OURS_SHEET, "A:E", col=0, header_rows=1):
            slug = _covered_slug(t)
            if slug:
                out.setdefault(slug, "ours")
    except Exception as e:
        errors.append(f"ours: {e}")
    try:
        for t in await _read_sheet_col(COVERED_COMPANY_SHEET, "A2:E", col=2, header_rows=1):
            name = _COVERED_COMPANY_PREFIX.sub("", t).strip()
            slug = _covered_slug(name)
            if slug:
                out.setdefault(slug, "company")
    except Exception as e:
        errors.append(f"company: {e}")
    try:
        pipeline_rows = await _sheet_read_all(COVERED_OURS_SHEET, "Pipeline")
        for row in pipeline_rows[1:]:
            if len(row) > 2:
                slug = _covered_slug(row[2])
                if slug:
                    out.setdefault(slug, "pipeline")
    except Exception as e:
        errors.append(f"pipeline: {e}")
    return out, errors


@router.get("/api/thailandnow/events/covered")
async def get_covered_events() -> dict:
    """Covered-events set for the EVENTS tab dedup badge. {covered: {slug: source}, errors: []}."""
    covered, errors = await _covered_events()
    return {"covered": covered, "errors": errors}


async def _wp_pull_published_events() -> list[dict]:
    """Page the public Thailand NOW WP REST event endpoint."""
    events: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as c:
        while True:
            url = f"https://www.thailandnow.in.th/wp-json/wp/v2/event?per_page=100&page={page}&_fields=slug,date,title,link,id"
            r = await c.get(url)
            if r.status_code != 200:
                break
            try:
                items = r.json()
            except Exception:
                break
            if not isinstance(items, list) or not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                t_val = item.get("title")
                title = t_val.get("rendered", "") if isinstance(t_val, dict) else str(t_val or "")
                title = html.unescape(title).strip()
                events.append({
                    "id": item.get("id"),
                    "date": item.get("date") or "",
                    "slug": item.get("slug") or "",
                    "link": item.get("link") or "",
                    "title": title,
                })
            if len(items) < 100:
                break
            page += 1
    return events


@router.post("/api/thailandnow/events/registry/sync")
async def sync_events_registry() -> dict:
    """Sync published events from WP to the OURS Covered Events Registry sheet (tab 1),
    and flip matched pipeline items to PUBLISHED."""
    events = await _wp_pull_published_events()
    if not events:
        # A dead/renamed WP endpoint must never blank the registry — skip the rewrite.
        return {"published_synced": 0, "pipeline_flipped": 0,
                "skipped": "WP pull returned no events — Published tab left untouched"}
    events.sort(key=lambda x: str(x.get("date") or ""))

    header = ["Event Title", "Date Published (WP)", "Slug", "WP Link", "WP ID"]
    new_rows: list[list[str]] = [header]
    for e in events:
        new_rows.append([
            str(e.get("title") or ""),
            str(e.get("date") or ""),
            str(e.get("slug") or ""),
            str(e.get("link") or ""),
            str(e.get("id") or ""),
        ])

    curr_rows: list[list[str]] = []
    try:
        curr_rows = await _sheet_read_all(COVERED_OURS_SHEET, "")
    except Exception:
        pass

    max_rows = max(len(new_rows), len(curr_rows) + 1)
    padded_rows: list[list[str]] = []
    for r in new_rows:
        padded_rows.append((r + [""] * 5)[:5])
    while len(padded_rows) < max_rows:
        padded_rows.append(["", "", "", "", ""])

    await _sheet_update_range(COVERED_OURS_SHEET, "", f"A1:E{max_rows}", padded_rows)

    published_slugs = set()
    for e in events:
        s1 = _covered_slug(e.get("slug") or "")
        if s1:
            published_slugs.add(s1)
        s2 = _covered_slug(e.get("title") or "")
        if s2:
            published_slugs.add(s2)

    pipeline_flipped = 0
    try:
        await _ensure_pipeline_tab()
        pipeline_rows = await _sheet_read_all(COVERED_OURS_SHEET, "Pipeline")
        for idx, row in enumerate(pipeline_rows, start=1):
            if idx == 1:
                continue
            row_slug = _covered_slug(row[2]) if len(row) > 2 else ""
            row_status = (row[5] if len(row) > 5 else "").strip()
            if row_slug and (row_slug in published_slugs) and row_status != "PUBLISHED":
                await _sheet_update_cell(COVERED_OURS_SHEET, "Pipeline", idx, "F", "PUBLISHED")
                pipeline_flipped += 1
    except Exception:
        pass

    return {"published_synced": len(events), "pipeline_flipped": pipeline_flipped}


def _fireside_source_gem_path() -> Path:
    """Fireside topic sourcing gem path."""
    return _resolve_gem("fireside_source_gem_path", "app/gems/fireside-source.md")


def _fireside_edit_gem_path() -> Path:
    """Fireside edit notes gem path."""
    return _resolve_gem("fireside_edit_gem_path", "app/gems/fireside-edit-notes.md")


async def _flow_fireside_source(job: TnJob, seed: str | None, category: str | None) -> None:
    """SOURCE TOPICS flow: query corpus notebook for fresh episode ideas, with relaxed
    web fallback on thin answer, followed by LLM shaping to strict JSON schema."""
    # 1. Topic registry lookup
    done_topics: list[str] = []
    revisitable: list[dict] = []
    try:
        registry = await _fireside_registry()
        done_topics, revisitable = _filter_fireside_registry(registry)
    except Exception as e:
        job.logs.append(f"registry fetch skipped/failed: {e}")

    # Extract unique, concise topic themes (most recent + core 35) to keep NLM query prompt fast
    unique_done = []
    for t in reversed(done_topics):
        clean = t.split(":")[0].split("—")[0].strip()
        if clean and clean not in unique_done:
            unique_done.append(clean)
    done_list_str = ", ".join(unique_done[:35]) if unique_done else "(none)"
    revisit_list_str = ", ".join([f"{r.get('ep', '')}: {r.get('topic', '')}" for r in revisitable if r.get("topic")]) or "(none)"

    # 2. Discover notebook (OPTIONAL — web fallback covers the no-notebook case,
    #    so SOURCE works before the corpus is built / if the notebooklm backend is down)
    nid = await _fireside_nid()
    job.notebook = nid or ""

    # 3. Ask corpus notebook (only when a notebook is configured)
    answer = ""
    refs: list[dict] = []
    mapped_urls: list[str] = []
    mode = "notebook"

    if nid:
        subject = (seed or category or "Thailand living, visas, travel and expat life").strip()
        # Ben's methodology: start from a concrete news/event hook, then broaden to 4 angles:
        # cultural dimension, industry/economic dimension, government policy, ASEAN comparison.
        # The hook is the excuse to talk about Thailand more broadly.
        # ALSO: avoid Queen/Mother's-Day topics per Ben's explicit instruction.
        prompt = (
            f"Suggest 3-5 FRESH episode topics on '{subject}' for The Fireside YouTube show. "
            "Follow Ben Rujopakarn's development methodology: each topic MUST start from a "
            "concrete, real, current NEWS OR EVENT hook (something happening NOW or in the "
            "coming weeks), then broaden into 3-4 development angles: (1) CULTURAL dimension, "
            "(2) INDUSTRY/ECONOMIC dimension, (3) GOVERNMENT POLICY dimension, (4) ASEAN/REGIONAL "
            "COMPARISON. The hook is the excuse to talk about Thailand more broadly. "
            f"HARD AVOID (do NOT suggest any topic on this list OR its themes): [{done_list_str}]. "
            "ALSO AVOID: any Queen-related or Mother's Day-related topics. "
            f"Revisitable update candidates (these COULD be revisited if a strong new hook exists): [{revisit_list_str}]. "
            "For each topic, provide:\n"
            "- Title (punchy YouTube-optimized)\n"
            "- Angle framed as the two questions a foreigner-in-Thailand asks\n"
            "- A real, verifiable news/event hook (name the specific event, report, or date if known)\n"
            "- Development angles: cultural / industry-economic / government policy / ASEAN comparison\n"
            "- Adjacent past episode #s or topics\n"
            "- 2-4 citable source URLs or references from the corpus\n"
            "- An 'If You Like A, Try B' pairing with a past episode\n"
            "- Visual/chapter-card style used for similar episodes\n"
            "- Why it is fresh and timely\n"
            "- Whether it is a revisit candidate update (boolean)"
        )

        source_map: dict[str, str] = {}
        try:
            src_data = await _run_cli(["nlm", "source", "list", nid, "--json"], timeout=40)
            src_list = src_data if isinstance(src_data, list) else (src_data.get("sources", []) if isinstance(src_data, dict) else [])
            for s in (src_list or []):
                if isinstance(s, dict):
                    sid = s.get("id") or s.get("source_id")
                    surl = s.get("url") or s.get("title") or ""
                    if sid and surl:
                        source_map[str(sid)] = str(surl)
        except Exception as e:
            job.logs.append(f"source list check: {e}")

        try:
            ask_res = await _run_cli(["nlm", "notebook", "query", nid, prompt, "--json"], timeout=180)
            if isinstance(ask_res, dict):
                answer = str(ask_res.get("answer") or ask_res.get("text") or "").strip()
                citations = ask_res.get("citations") or ask_res.get("references") or {}
                # nlm returns citations as {num: source_id}; legacy shape was a list of dicts
                if isinstance(citations, dict):
                    cite_sids = [str(v) for v in citations.values()]
                elif isinstance(citations, list):
                    cite_sids = [str(r.get("source_id") or r.get("id") or "") for r in citations if isinstance(r, dict)]
                else:
                    cite_sids = []
                refs = cite_sids
                for sid in cite_sids:
                    if sid in source_map and source_map[sid]:
                        mapped_urls.append(source_map[sid])
        except Exception as e:
            job.logs.append(f"corpus ask error: {e}")
    else:
        job.logs.append("no Fireside corpus notebook — using web fallback")

    # 4. ASK-THIN GUARD: if answer empty or (no refs and no mapped urls) -> relaxed web fallback
    if not answer or (not refs and not mapped_urls):
        job.logs.append("corpus answer thin or empty; triggering relaxed web fallback")
        q_clean = (seed or category or "Thailand foreigner living").strip()
        queries = [
            f"{q_clean}",
            f"Thailand {q_clean} news",
            f"ประเทศไทย {q_clean}",
            f"{q_clean} site:thairath.co.th OR site:khaosod.co.th OR site:matichon.co.th OR site:prachachat.net",
        ]
        ddg_urls: list[str] = []
        for q in queries:
            try:
                md = await _jina_read(f"https://duckduckgo.com/html/?q={urllib.parse.quote(q)}")
                for ev in _parse_ddg(md):
                    ddg_urls.append(ev["url"])
            except Exception:
                pass

        brave_results = await asyncio.gather(*[_brave_urls(q) for q in queries])
        brave_urls = [u for batch in brave_results for u in batch]
        gnews_urls = await _gnews_urls(f"{q_clean} news")

        urls = _scout_dedup([*ddg_urls, *brave_urls, *gnews_urls], by_domain=True, limit=20)

        async def _fx(u: str):
            try:
                md = await _jina_read(u)
                return _extract_news(md, u)
            except Exception:
                return None

        extracted = await asyncio.gather(*[_fx(u) for u in urls])
        web_results = [r for r in extracted if r]  # keep undated (no date filter)
        web_results = await _scout_rerank(web_results)

        answer = "\n\n".join([
            f"Title: {r.get('title')}\nURL: {r.get('url')}\nSnippet: {r.get('snippet') or r.get('excerpt')}"
            for r in web_results[:10]
        ])
        mapped_urls = [r.get("url") for r in web_results if r.get("url")]
        mode = "web-fallback"

    # 5a. WEB-VERIFY HOOKS + collect real source URLs (Critical: corpus can fabricate specific dates/events)
    # For each topic the corpus proposed, verify the hook via live web search.
    # This runs on BOTH the notebook path and the web-fallback path.
    if answer:
        job.logs.append("running hook web-verification pass...")
        # Extract candidate topic hooks for verification (parse from corpus answer text)
        hook_queries: list[str] = []
        # Simple heuristic: extract lines starting with "hook", "event", or title-like capitalized lines
        for line in answer.split("\n"):
            l = line.strip().lstrip("-*•").strip()
            llow = l.lower()
            if (llow.startswith("hook") or llow.startswith("event") or llow.startswith("news")) and len(l) > 20:
                hook_queries.append(l[:120])
            elif len(l) > 20 and l[0].isupper() and not l.startswith("For") and not l.startswith("The topic"):
                hook_queries.append(l[:100])
        hook_queries = hook_queries[:6]  # cap to avoid burn

        # Also add subject as baseline
        hook_queries.append(f"{(seed or category or subject)} Thailand 2025 2026")

        web_verify_urls: list[str] = []
        async def _vfx(q: str) -> list[str]:
            out: list[str] = []
            try:
                brs = await _brave_urls(q)
                out.extend(brs[:3])
            except Exception:
                pass
            try:
                gns = await _gnews_urls(q)
                out.extend(gns[:2])
            except Exception:
                pass
            return out

        verify_batches = await asyncio.gather(*[_vfx(q) for q in hook_queries])
        for batch in verify_batches:
            web_verify_urls.extend(batch)

        # Dedup and add verified URLs to mapped_urls pool (they are real, current)
        web_verify_deduped = _scout_dedup([u for u in web_verify_urls if u and "youtube.com" not in u], by_domain=False, limit=20)
        for u in web_verify_deduped:
            if u not in mapped_urls:
                mapped_urls.append(u)

        job.logs.append(f"web-verify found {len(web_verify_deduped)} real source URLs")

    # 5b. Corpus episode-tie: always query corpus for adjacent past episodes, even on web-fallback path
    if nid:
        subject_for_tie = (seed or category or "general Thailand topic").strip()
        try:
            tie_res = await _run_cli(
                ["nlm", "notebook", "query", nid,
                 f"Which past Fireside episode(s) are most topically adjacent to '{subject_for_tie}'? "
                 f"Give the EP# and episode title for each match. If none, say 'no close past episode'.",
                 "--json"], timeout=120
            )
            if isinstance(tie_res, dict):
                tie_answer = str(tie_res.get("answer") or "").strip()
                if tie_answer:
                    # Append tie context into the user prompt for the shaping step
                    answer = answer + f"\n\n[EPISODE TIES FROM CORPUS]\n{tie_answer}"
                    job.logs.append(f"episode tie query: {tie_answer[:120]}")
        except Exception as e:
            job.logs.append(f"episode tie query failed: {e}")

    # 5c. Shape pass
    system = _load_gem(_fireside_source_gem_path())
    opts = _opts()
    model = (opts.get("fireside_llm") or opts.get("scout_llm") or {}).get("model") or "glm-5"
    # glm-5 returns prose (not JSON) when fed the full corpus answer + 20 URLs — cap the
    # context and force a JSON directive in the USER prompt (last thing the model sees).
    answer_trimmed = (answer or "")[:2500]
    urls_trimmed = (mapped_urls or [])[:12]
    user_prompt = (
        "Respond with ONLY a valid JSON object: {\"topics\":[...]} with 3-5 topic objects "
        "matching the schema in the system prompt. NO prose, NO markdown, NO code fences. "
        "Begin your response with { and end with }.\n\n"
        f"Seed: {seed or ''}\nCategory: {category or ''}\n\n"
        f"Findings (corpus + web + episode ties):\n{answer_trimmed}\n\n"
        f"Available source URLs:\n" + "\n".join(urls_trimmed)
    )

    try:
        raw = await zai_message(user_prompt, max_tokens=8192, system=system, model=model, timeout=180)
        parsed = _parse_json_lenient(raw)
        if isinstance(parsed, dict) and "topics" in parsed:
            raw_topics = parsed["topics"]
        elif isinstance(parsed, list):
            raw_topics = parsed
        elif isinstance(parsed, dict) and ("title" in parsed or "hook" in parsed):
            # glm-5 sometimes returns a single flat topic object instead of {"topics":[…]} — recover it
            raw_topics = [parsed]
            job.logs.append("shaping: model returned a single topic (not wrapped) — recovered")
        else:
            raw_topics = []
            job.logs.append(f"shaping: no topics recovered — parsed={type(parsed).__name__}; keys={list(parsed.keys())[:10] if isinstance(parsed, dict) else (str(parsed)[:80] if not isinstance(parsed,(list,dict)) else 'list/other')}; raw_len={len(str(raw))}")
    except Exception as e:
        job.logs.append(f"shaping pass failed: {e}")
        raw_topics = []

    topics = []
    for t in (raw_topics if isinstance(raw_topics, list) else []):
        if not isinstance(t, dict):
            continue
        ep_adj = t.get("ep_adjacent") or t.get("adjacent") or t.get("episode_tie")
        ep_adj_list = [str(x).strip() for x in ep_adj if x] if isinstance(ep_adj, list) else ([str(ep_adj).strip()] if ep_adj else [])
        src_urls = t.get("source_urls") or t.get("sources") or []
        src_urls_list = [str(x).strip() for x in src_urls if x] if isinstance(src_urls, list) else ([str(src_urls).strip()] if src_urls else [])
        topics.append({
            "title": str(t.get("title") or t.get("episode_title") or "").strip(),
            "angle": str(t.get("angle") or t.get("angle framing") or t.get("angle_framing") or "").strip(),
            "hook": str(t.get("hook") or "").strip(),
            "development_angles": t.get("development_angles") if isinstance(t.get("development_angles"), dict) else {},
            "ep_adjacent": ep_adj_list,
            "source_urls": src_urls_list,
            "if_like_a_try_b": str(t.get("if_like_a_try_b") or "").strip(),
            "visual_style": str(t.get("visual_style") or "").strip(),
            "why_fresh": str(t.get("why_fresh") or "").strip(),
            "revisit_candidate": bool(t.get("revisit_candidate", False)),
            "hook_unverified": bool(t.get("hook_unverified", False)),
        })

    topics, neg_dropped = _screen_negative(topics)
    if neg_dropped:
        job.logs.append(f"negative-framing screen dropped {neg_dropped} topic(s)")

    # 6. Set job result
    job.result = {
        "topics": topics,
        "mode": mode,
        "notebook_id": nid,
    }


@router.post("/api/thailandnow/scout/fireside/source")
async def scout_fireside_source(payload: dict = Body(default={})):
    """STORY SCOUT — The Fireside topic sourcing (single-flight async job)."""
    body = payload or {}
    seed = body.get("seed")
    category = body.get("category")
    if any(j.kind == "fireside-source" and j.status in _TN_RUNNING for j in _TN_JOBS.values()):
        raise HTTPException(409, "a FIRESIDE topic sourcing job is already running")
    label = f"fireside-source: {(seed or category or 'general')[:40]}"
    return _tn_spawn(
        "fireside-source",
        label,
        lambda j: _flow_fireside_source(j, seed, category),
    )


@router.get("/api/thailandnow/scout/fireside/source/report/{jid}")
async def scout_fireside_source_report(jid: str):
    """Fetch results of a completed FIRESIDE topic sourcing job."""
    job = _TN_JOBS.get(jid)
    if not job or job.kind != "fireside-source":
        raise HTTPException(404, "no such FIRESIDE topic sourcing job")
    if job.status != "done":
        raise HTTPException(409, f"job is {job.status}; not ready")
    return job.result


async def _fireside_edit(
    draft: str | None = None,
    url: str | None = None,
    check_coverage: bool = False,
) -> dict:
    """Generate editorial notes for a draft episode script in Ben Rujopakarn's voice."""
    if draft and str(draft).strip():
        text = str(draft).strip()
    elif url and str(url).strip():
        try:
            text = (await _jina_read(str(url).strip())).strip()
        except Exception:
            text = ""
        if not text or len(text) < 50:
            return {"notes": {}, "mode": "degraded", "error": "paste the draft — couldn't read the URL"}
    else:
        raise HTTPException(400, "provide draft text or a document url")

    gem_path = _fireside_edit_gem_path()
    system = _load_gem(gem_path)
    opts = _opts()
    model = (opts.get("fireside_llm") or opts.get("scout_llm") or {}).get("model") or "glm-5"

    try:
        raw = await zai_message(text, max_tokens=8192, system=system, model=model, timeout=180)
        notes = _parse_json_lenient(raw) or {}
        mode = "direct"
    except Exception:
        notes, mode = {}, "degraded"

    if check_coverage and mode == "direct":
        try:
            nid = await _fireside_nid()
            if nid:
                cov_prompt = (
                    "Has this episode topic / angle been covered in past episodes? Which episode numbers (EP#) or runs? "
                    f"Draft excerpt / summary:\n\n{text[:3000]}"
                )
                cov_res = await _run_cli([CLI, "ask", cov_prompt, "--json", "--notebook", nid], timeout=60)
                cov_answer = cov_res.get("answer") or cov_res.get("text") or ""
                if isinstance(notes, dict):
                    notes["coverage_check"] = str(cov_answer).strip()
            else:
                if isinstance(notes, dict):
                    notes.setdefault("coverage_check", "")
        except Exception:
            if isinstance(notes, dict):
                notes.setdefault("coverage_check", "")
    else:
        if isinstance(notes, dict):
            notes.setdefault("coverage_check", "")

    return {"notes": notes, "mode": mode}


@router.post("/api/thailandnow/scout/fireside/edit-notes")
async def scout_fireside_edit_notes(payload: dict = Body(default={})):
    """STORY SCOUT — The Fireside editorial notes on a draft script."""
    body = payload or {}
    draft = body.get("draft")
    url = body.get("url")
    check_coverage = bool(body.get("check_coverage", False))
    return await _fireside_edit(draft=draft, url=url, check_coverage=check_coverage)


# --- FIRESIDE IDE lane (prompt-out + file-in; contract: 10-knowledge/fireside-ide-handoff.md) ---
# Same pattern as the EVENTS IDE lane: 📋 copies a paste-ready Antigravity prompt,
# the IDE agent scouts + writes JSON to _FIRESIDE_IDE_HANDOFF, ⇄ CONVERT reads it back.
_FIRESIDE_IDE_HANDOFF = Path("/tmp/railjack-fireside/latest.json")


def _fireside_topic_row(it: dict) -> dict | None:
    """Coerce ONE IDE handoff row into the FiresideTopic card shape (or None when
    unusable). Strings are trimmed/capped; list fields tolerate a single string."""
    title = str(it.get("title") or "").strip()
    if not title:
        return None  # title is the dedup key + React key — must be present

    def _strs(v) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        s = str(v or "").strip()
        return [s] if s else []

    return {
        "title": title[:200],
        "angle": str(it.get("angle") or "").strip(),
        "ep_adjacent": _strs(it.get("ep_adjacent")),
        "source_urls": _strs(it.get("source_urls")),
        "if_like_a_try_b": str(it.get("if_like_a_try_b") or "").strip(),
        "visual_style": str(it.get("visual_style") or "").strip(),
        "why_fresh": str(it.get("why_fresh") or "").strip(),
        "revisit_candidate": bool(it.get("revisit_candidate", False)),
    }


@router.get("/api/thailandnow/scout/fireside/ide-prompt")
async def fireside_ide_prompt(seed: str = "", category: str = ""):
    """📋 IDE SOURCE — build the paste-ready Antigravity prompt. The done-topic list
    from the registry is INLINED so the IDE run doesn't waste topics on episodes
    already shipped. Registry failure degrades to the fixed avoids only."""
    done_topics: list[str] = []
    try:
        done_topics, _ = _filter_fireside_registry(await _fireside_registry())
    except Exception:
        pass
    unique_done: list[str] = []
    for t in reversed(done_topics):
        clean = t.split(":")[0].split("—")[0].strip()
        if clean and clean not in unique_done:
            unique_done.append(clean)
    done_list = ", ".join(unique_done[:60]) or "(registry unreadable — rely on the fixed avoids)"
    subject = (seed.strip() or category.strip() or "Thailand living, visas, travel and expat life")
    prompt = (
        "Read `10-knowledge/fireside-ide-handoff.md` in this vault. "
        f"Suggest 3-5 FRESH episode topics on '{subject}' for The Fireside (NBT World's weekly "
        "two-host YouTube show; audience = foreigners living in or visiting Thailand). Follow Ben "
        "Rujopakarn's methodology: every topic MUST start from a concrete, real, CURRENT news or "
        "event hook, then broaden into 3-4 development angles: (1) cultural, (2) industry/economic, "
        "(3) government policy, (4) ASEAN/regional comparison — the hook is the excuse to talk "
        "about Thailand more broadly. Use real web browsing/search to verify the hooks and gather "
        "2-4 citable source URLs per topic. HARD AVOID (never suggest these topics or their "
        f"themes): [{done_list}]. ALSO AVOID: Queen-related and Mother's Day topics, "
        "AND negative-framing angles (tourist-trap / myth-bust / warning pieces, crackdown "
        "stories, skip-avoid listicles) — frame constructively or pick another topic. "
        "Write the result to `/tmp/railjack-fireside/latest.json` in the EXACT JSON shape from "
        "that contract note. Do NOT create or edit any Google Sheet/doc — the hub panel's "
        "CONVERT handles registry coverage flags."
    )
    return {"text": prompt, "done_count": len(unique_done)}


@router.get("/api/thailandnow/scout/fireside/convert")
async def fireside_ide_convert():
    """⇄ CONVERT — read the IDE handoff (``_FIRESIDE_IDE_HANDOFF``), coerce rows to
    the FiresideTopic shape, dedup on title slug, and mark ``covered: true`` on
    topics whose slug matches a done/excluded registry topic. Keyless, no LLM.
    Soft-fails to HTTP 200 ``{topics: [], errors: […]}`` pointing at 📋 IDE SOURCE."""
    missing = {"topics": [], "count": 0, "covered": 0,
               "errors": ["no IDE handoff file — run 📋 IDE SOURCE first"]}
    p = _FIRESIDE_IDE_HANDOFF
    if not p.exists():
        return missing
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("topics") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not isinstance(rows, list):
            rows = []
    except Exception:
        return {**missing, "errors": ["handoff isn't valid JSON — rerun 📋 IDE SOURCE"]}

    done_slugs: set[str] = set()
    try:
        done_topics, _ = _filter_fireside_registry(await _fireside_registry())
        done_slugs = {s for t in done_topics if (s := _covered_slug(t))}
    except Exception:
        pass  # registry down → no covered flags, topics still convert

    seen: set[str] = set()
    topics: list[dict] = []
    covered = 0
    for it in rows:
        if not isinstance(it, dict):
            continue
        row = _fireside_topic_row(it)
        if not row:
            continue
        slug = _covered_slug(row["title"])
        if not slug or slug in seen:
            continue
        seen.add(slug)
        row["covered"] = slug in done_slugs
        if row["covered"]:
            covered += 1
        topics.append(row)
    topics, neg_dropped = _screen_negative(topics)
    return {"topics": topics, "count": len(topics), "covered": covered,
            "negative_dropped": neg_dropped, "mtime": p.stat().st_mtime, "errors": []}



_EVENTS_HANDOFF = Path("/tmp/railjack-events/latest.json")


@router.get("/api/thailandnow/events/terminal-report")
async def events_terminal_report():
    """CONVERT — read the JSON the /events-scout skill wrote to disk. Returns
    {events, count, mtime} in the TnEvent card shape. 404 until the skill has
    written a file; 422 if the handoff isn't a valid JSON array."""
    p = _EVENTS_HANDOFF
    if not p.exists():
        raise HTTPException(404, "no events handoff yet — run SCOUT ▸ CLAUDE, let Claude finish (writes /tmp/railjack-events/latest.json)")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(422, f"handoff isn't valid JSON: {e}")
    if not isinstance(raw, list):
        raise HTTPException(422, "handoff must be a JSON array")
    events = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        u = str(it.get("url") or "").strip()
        if not u:
            continue  # url is the React key + dedup key — must be present & unique
        events.append({
            "title":           str(it.get("title") or u).strip(),
            "url":             u,
            "start_date":      str(it.get("start_date") or "").strip(),
            "end_date":        str(it.get("end_date") or "").strip(),
            "signup_deadline": str(it.get("signup_deadline") or "").strip(),
            "location":        str(it.get("location") or "").strip(),
            "language":        str(it.get("language") or "").strip(),
            "summary":         str(it.get("summary") or "").strip(),
            "source":          str(it.get("source") or "").strip(),
        })
    # dedup on url, preserve order (dup urls would collide as React keys)
    seen, deduped = set(), []
    for e in events:
        if e["url"] not in seen:
            seen.add(e["url"])
            deduped.append(e)
    return {"events": deduped, "count": len(deduped), "mtime": p.stat().st_mtime}


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


_HARVEST_BLOCK_SUBSTR = (
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "tiktok.com", "linkedin.com", "mailto:", "tel:", "javascript:", "/login",
    "/signin", "/register", "/cart", "/checkout", "/wp-admin", "/wp-content",
    "/feed", ".pdf", ".jpg", ".jpeg", ".png", ".webp", "/tag/", "/category/",
    "/author/", "/page/",
)
_HARVEST_BLOCK_TEXT = {
    "home", "about", "about us", "contact", "contact us", "menu", "search",
    "login", "log in", "sign in", "sign up", "register", "next", "previous",
    "load more", "read more", "see more", "view all", "all events", "all news",
    "events", "news", "back", "skip to content", "subscribe", "newsletter",
    "privacy", "terms", "cookie", "english", "ภาษาไทย", "ไทย",
}


# --- Inline-event extraction (listicle pages: many events as H3/H4 headings on ONE
# url, no per-event links) + a 5-day-past cutoff so stale events are dropped. ---
_HUB_CUTOFF_DAYS = 5
_INLINE_MONTH = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _inline_month_num(tok: str) -> int | None:
    t = tok.lower()
    return _INLINE_MONTH.get(t[:4]) or _INLINE_MONTH.get(t[:3])


def _last_day_of_month(y: int, m: int) -> int:
    nxt = datetime(y, m, 28) + timedelta(days=4)
    return (nxt - timedelta(days=nxt.day)).day


def _parse_inline_date(text: str, ctx_year: int) -> tuple[str, str] | None:
    """Parse (start_iso, end_iso) from listicle-style date text. Handles ranges
    ('August 7–9, 2026', '7 to 9 August'), single days with/without year, and
    month-only / month-range ('August 2026', 'June to August 2026' → end month's
    last day so ongoing months aren't dropped). None if nothing parses."""
    if not text:
        return None
    t = text.lower()
    pats = [
        (r"([a-z]{3,9})\s+(\d{1,2})\s*[—–\-]\s*(\d{1,2})(?:[^\d]{0,4}(\d{4}))?", "mdd"),
        (r"([a-z]{3,9})\s+(\d{1,2})\s+to\s+(\d{1,2})(?:[^\d]{0,4}(\d{4}))?", "mdd"),
        (r"(\d{1,2})\s*[—–\-]\s*(\d{1,2})\s+([a-z]{3,9})(?:[^\d]{0,4}(\d{4}))?", "ddm"),
        (r"(\d{1,2})\s+to\s+(\d{1,2})\s+([a-z]{3,9})(?:[^\d]{0,4}(\d{4}))?", "ddm"),
        (r"([a-z]{3,9})\s+(\d{1,2})(?:[^\d]{0,4}(\d{4}))?", "md"),
        (r"(\d{1,2})\s+([a-z]{3,9})(?:[^\d]{0,4}(\d{4}))?", "dm"),
        (r"([a-z]{3,9})\s+to\s+([a-z]{3,9})(?:[^\d]{0,4}(\d{4}))?", "mm"),
        (r"([a-z]{3,9})\s+(\d{4})", "my"),
    ]
    for pat, kind in pats:
        m = re.search(pat, t)
        if not m:
            continue
        g = m.groups()
        try:
            if kind in ("mdd", "ddm"):
                if kind == "mdd":
                    mo, d1, d2, yr = g
                else:
                    d1, d2, mo, yr = g
                mn = _inline_month_num(mo)
                if not mn:
                    continue
                y = int(yr) if yr else ctx_year
                return (f"{y:04d}-{mn:02d}-{int(d1):02d}",
                        f"{y:04d}-{mn:02d}-{int(d2):02d}")
            if kind == "md":
                mo, d, yr = g
                mn = _inline_month_num(mo)
                if not mn:
                    continue
                y = int(yr) if yr else ctx_year
                ds = f"{y:04d}-{mn:02d}-{int(d):02d}"
                return (ds, ds)
            if kind == "dm":
                d, mo, yr = g
                mn = _inline_month_num(mo)
                if not mn:
                    continue
                y = int(yr) if yr else ctx_year
                ds = f"{y:04d}-{mn:02d}-{int(d):02d}"
                return (ds, ds)
            if kind == "mm":
                mo1, mo2, yr = g
                mn1, mn2 = _inline_month_num(mo1), _inline_month_num(mo2)
                if not (mn1 and mn2):
                    continue
                y = int(yr) if yr else ctx_year
                return (f"{y:04d}-{mn1:02d}-01",
                        f"{y:04d}-{mn2:02d}-{_last_day_of_month(y, mn2):02d}")
            if kind == "my":
                mo, yr = g
                mn = _inline_month_num(mo)
                if not mn:
                    continue
                y = int(yr)
                return (f"{y:04d}-{mn:02d}-01",
                        f"{y:04d}-{mn:02d}-{_last_day_of_month(y, mn):02d}")
        except ValueError:
            continue
    return None


def _extract_inline_events(md: str, source_url: str) -> list[dict]:
    """Extract events listed inline (H3/H4 headings + description) from one listicle
    page. H2 = category (skip); scan stops at FAQ/Related/Comments. Drops events that
    ended > _HUB_CUTOFF_DAYS ago; keeps undated (can't prove past) + upcoming. url is
    set "" so the frontend keys by title (not the shared page url)."""
    today = datetime.now()
    cutoff = (today - timedelta(days=_HUB_CUTOFF_DAYS)).strftime("%Y-%m-%d")
    ctx_year = today.year
    mym = re.search(r"\b(20[2-3]\d)\b", md[:800])
    if mym:
        ctx_year = int(mym.group(1))

    lines = md.split("\n")
    cutoff_line = len(lines)
    for i, ln in enumerate(lines):
        if re.match(r"^#{1,3}\s*(FAQ|Frequently Asked|Related (Posts|Articles)|Comments|"
                    r"Leave a|About the Author|Share this|You may also like)", ln, re.I):
            cutoff_line = i
            break

    out: list[dict] = []
    i = 0
    n = min(cutoff_line, len(lines))
    while i < n:
        m = re.match(r"^(#{3,4})\s+(.+)$", lines[i])
        if not m:
            i += 1
            continue
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        buf: list[str] = []
        j = i + 1
        while j < n and not re.match(r"^#{1,6}\s", lines[j]):
            buf.append(lines[j])
            j += 1
        section = title + "\n" + "\n".join(buf)
        desc = re.sub(r"\s+", " ",
                      re.sub(r"!\[[^\]]*\]\([^)]*\)", "", section)).strip()
        parsed = _parse_inline_date(section, ctx_year)
        if parsed:
            start_iso, end_iso = parsed
            if end_iso < cutoff:
                i = j
                continue  # ended >5 days ago — omit
            out.append({"title": title, "start_date": start_iso, "end_date": end_iso,
                        "signup_deadline": "", "location": "", "language": "en",
                        "summary": desc[:300], "url": "", "source": "inline"})
        elif len(desc) >= 60:  # undated but has a real description — keep
            out.append({"title": title, "start_date": "", "end_date": "",
                        "signup_deadline": "", "location": "", "language": "en",
                        "summary": desc[:300], "url": "", "source": "inline"})
        i = j
    return out


def _extract_link_events(md: str, url: str) -> list[dict]:
    """Same-origin ``[text](url)`` link harvest (the 'links' mode). Returns
    ``[{title, url}]`` scored by event-likeness, capped at 40. Shared by
    /deep/harvest and /events/hubs/scan."""
    base = urllib.parse.urlparse(url)
    base_host = (base.hostname or "").removeprefix("www.")
    base_abs = f"{base.scheme}://{base.netloc}"
    raw: dict[str, str] = {}
    for m in re.finditer(r"(?<!!)\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)", md):
        text = re.sub(r"\s+", " ", m.group(1)).strip()
        href = m.group(2).strip()
        absu = urllib.parse.urljoin(base_abs, href)
        pu = urllib.parse.urlparse(absu)
        if (pu.hostname or "").removeprefix("www.") != base_host:
            continue
        if len(text) < 5 or text.lower() in _HARVEST_BLOCK_TEXT:
            continue
        if any(b in absu.lower() for b in _HARVEST_BLOCK_SUBSTR):
            continue
        raw.setdefault(absu, text)

    def _score(u: str) -> int:
        p = urllib.parse.urlparse(u).path.lower()
        s = 6 if "event" in p else 0
        s += 3 if re.search(r"/20\d{2}", p) else 0
        s += min(len([x for x in p.split("/") if x]), 4)  # deeper path ≈ real page
        return s

    items = sorted(raw.items(), key=lambda kv: _score(kv[0]), reverse=True)[:40]
    return [{"title": t, "url": u} for u, t in items]


@router.post("/api/thailandnow/deep/harvest")
async def deep_harvest(payload: dict = Body(default={})):
    """Harvest events from a page. mode 'links' (default) = same-origin ``[text](url)``
    links off a listings/index page; mode 'events' = inline events listed as H3/H4
    headings on a single listicle page (many events, one url, no per-event links),
    with a 5-day-past cutoff. Returns ``{events, count, mode}``."""
    url = (payload.get("url") or "").strip()
    mode = (payload.get("mode") or "links").strip().lower()
    if not url:
        raise HTTPException(400, "url required")
    base = urllib.parse.urlparse(url)
    base_host = (base.hostname or "").removeprefix("www.")
    if not base_host:
        raise HTTPException(400, "url has no host")

    md = await _jina_read(url)

    if mode == "events":
        events = _extract_inline_events(md, url)
        return {"events": events, "count": len(events), "mode": "events"}

    events = _extract_link_events(md, url)
    return {"events": events, "count": len(events), "mode": "links"}


# --- Event-hub library: saved source pages (listing + listicle) for quick
# re-scanning while scouting. Local JSON store (personal scraping aid). ---

def _hubs_path() -> Path:
    return Path(os.path.expanduser("~/.config/railjack/event_hubs.json"))


def _read_hubs() -> list[dict]:
    p = _hubs_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text() or "[]")
    except Exception:
        return []


def _write_hubs(hubs: list[dict]) -> None:
    p = _hubs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hubs, ensure_ascii=False, indent=2))


@router.get("/api/thailandnow/events/hubs")
def hubs_list() -> dict:
    return {"hubs": _read_hubs()}


@router.post("/api/thailandnow/events/hubs")
async def hubs_add(payload: dict = Body(default={})) -> dict:
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    title = (payload.get("title") or url)[:120]
    mode = "events" if (payload.get("mode") or "").strip().lower() == "events" else "links"
    hubs = _read_hubs()
    if not any(h.get("url") == url for h in hubs):
        hubs.append({"url": url, "title": title, "mode": mode,
                     "added": datetime.now().strftime("%Y-%m-%d")})
        _write_hubs(hubs)
    return {"hubs": hubs}


@router.delete("/api/thailandnow/events/hubs")
async def hubs_remove(url: str) -> dict:
    hubs = [h for h in _read_hubs() if h.get("url") != url]
    _write_hubs(hubs)
    return {"hubs": hubs}


@router.post("/api/thailandnow/events/hubs/scan")
async def hubs_scan() -> dict:
    """Re-harvest every saved hub (by its stored mode) and merge into one event list.
    Inline-mode hubs are already date-filtered; the ✓ COVERED badge is applied
    client-side, so scanned events get marked automatically."""
    hubs = _read_hubs()
    merged: dict[str, dict] = {}  # keyOf (url||title) -> event (dedupe)
    errors: list[str] = []
    for h in hubs:
        url = h.get("url", "")
        mode = "events" if h.get("mode") == "events" else "links"
        try:
            md = await _jina_read(url)
            evs = (_extract_inline_events(md, url) if mode == "events"
                   else _extract_link_events(md, url))
            for e in evs:
                k = e.get("url") or e.get("title") or ""
                if k:
                    merged.setdefault(k, e)
        except Exception as ex:  # one bad hub shouldn't abort the scan
            errors.append(f"{url}: {ex}")
    events = list(merged.values())
    return {"events": events, "count": len(events),
            "hubs_scanned": len(hubs), "errors": errors}


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
    """Publicity gem path."""
    return _resolve_gem("gem_path", "app/gems/event-publicity.md")


def _extract_gem_body(text: str) -> str:
    """Extract the '## Role & Purpose' … '\\n---\\n' prompt body from raw gem text.

    Hardened against the intro/frontmatter trap (port bug 2026-07-29): an earlier
    gem — or its intro notes — that mentioned the literal '## Role & Purpose' made
    the old plain ``text.find()`` grab the wrong spot → garbage system prompt. We
    drop leading YAML frontmatter and match the heading only when it STARTS a
    line, so an inline mention (e.g. inside backticks in the notes) can't win.
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].lstrip()
    m = re.search(r"(?m)^##\s+Role\s*&\s*Purpose\b", text)
    body = text[m.start():] if m else text
    cut = body.find("\n---\n")
    if cut != -1:
        body = body[:cut]
    return body.strip()


def _load_gem(path: Path) -> str:
    """Read a system-prompt gem file and return its extracted prompt body
    (see _extract_gem_body)."""
    return _extract_gem_body(path.read_text(encoding="utf-8"))


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
    res = await provision({
        "desk_id": "tian",
        "title": title,
        "body": body.get("bundle_text") or "",
        "card_desc": "\n".join(desc_lines),
        "doc_name": '[{yyyymm}] [EN] "{title}"',
        "card_name": "Event | {title}",
        "due": body.get("due") or due_iso,
        "start": body.get("start") or start_iso,
    })
    if isinstance(res, dict) and res.get("items"):
        try:
            item0 = res["items"][0]
            card_url = item0.get("card_url", "")
            doc_url = item0.get("doc_url", "")
            today_iso = datetime.now().strftime("%Y-%m-%d")
            await _ensure_pipeline_tab()
            await _sheet_append_rows(
                COVERED_OURS_SHEET,
                "Pipeline",
                [[today_iso, title, _covered_slug(title), card_url, doc_url, "PIPELINE"]],
            )
            res["registry"] = "pipeline-logged"
        except Exception as e:
            res["registry"] = f"skipped: {e}"
    return res


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


def _archive_union(doc_lists: list[list[dict]]) -> list[dict]:
    """Dedupe Docs by id (a Doc can have >1 parent), sort newest-first by
    modifiedTime. Pure — no I/O — so the __main__ self-check can assert it."""
    seen: set[str] = set()
    out: list[dict] = []
    for docs in doc_lists:
        for d in docs:
            doc_id = d.get("id", "")
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                out.append(d)
    out.sort(key=lambda d: d.get("modifiedTime", ""), reverse=True)
    return out


async def _drive_list_event_docs(token: str) -> list[dict]:
    """List the Event Drive's Google Docs (newest first), recursively walking
    subfolders up to depth 5, paginating on nextPageToken, 90s-TTL-cached.
    Returns [{id,name,modifiedTime,webViewLink}]."""
    global _ARCHIVE_LIST_CACHE, _ARCHIVE_LIST_CACHE_AT
    if _ARCHIVE_LIST_CACHE is not None and \
            (time.monotonic() - _ARCHIVE_LIST_CACHE_AT) < _ARCHIVE_LIST_TTL_S:
        return _ARCHIVE_LIST_CACHE
    folder = _archive_folder_id()
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    doc_lists: list[list[dict]] = []

    async with httpx.AsyncClient(timeout=30) as c:
        folder_queue: list[tuple[str, int]] = [(folder, 0)]

        while folder_queue:
            cur_folder_id, depth = folder_queue.pop(0)

            # 1. Fetch Docs in cur_folder_id
            cur_docs: list[dict] = []
            page_token: str | None = None
            while True:
                params = {
                    "q": f"'{cur_folder_id}' in parents and "
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
                    cur_docs.append({
                        "id": f.get("id", ""), "name": f.get("name", ""),
                        "modifiedTime": f.get("modifiedTime", ""),
                        "webViewLink": f.get("webViewLink", ""),
                    })
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            if cur_docs:
                doc_lists.append(cur_docs)

            # 2. Fetch child subfolders if depth < 5
            if depth < 5:
                sub_token: str | None = None
                while True:
                    sub_params = {
                        "q": f"'{cur_folder_id}' in parents and "
                        "mimeType='application/vnd.google-apps.folder' and trashed=false",
                        "fields": "files(id,name),nextPageToken",
                        "pageSize": 200,
                        "supportsAllDrives": "true",
                    }
                    if sub_token:
                        sub_params["pageToken"] = sub_token
                    r_sub = await c.get("https://www.googleapis.com/drive/v3/files",
                                        headers=hdr, params=sub_params)
                    if r_sub.status_code != 200:
                        raise HTTPException(502, f"Drive subfolder list failed: {r_sub.status_code} {r_sub.text[:200]}")
                    sub_data = r_sub.json()
                    for sf in sub_data.get("files", []):
                        sf_id = sf.get("id")
                        if sf_id:
                            folder_queue.append((sf_id, depth + 1))
                    sub_token = sub_data.get("nextPageToken")
                    if not sub_token:
                        break

    out = _archive_union(doc_lists)
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


async def _drive_read_doc_html(token: str, doc_id: str) -> str:
    """Export a Google Doc as HTML (mimeType=text/html). Returns empty string if export fails."""
    hdr = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://www.googleapis.com/drive/v3/files/{doc_id}/export",
            headers=hdr, params={"mimeType": "text/html"},
        )
        if r.status_code != 200:
            return ""
        return r.text


async def _drive_read_doc_json(token: str, doc_id: str) -> dict:
    """Fetch structured Google Doc AST via Docs API v1 (https://docs.googleapis.com/v1/documents/{doc_id}).
    Returns dict AST or empty dict on error."""
    hdr = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.get(f"https://docs.googleapis.com/v1/documents/{doc_id}", headers=hdr)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {}


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


def _archive_is_detail(question: str) -> bool:
    """Synthesis/detail intent — wants specific info about an event (when/where/
    how much). Stage 2 answers these via LLM synthesis over the top docs."""
    return bool(_ARCHIVE_DETAIL.search(question.lower()))


def _archive_gem_path() -> Path:
    """ARCHIVE Q&A gem path."""
    return _resolve_gem("archive_gem_path", "app/gems/event-archive-qa.md")


@router.post("/api/thailandnow/archive/ask")
async def archive_ask(payload: dict = Body(default={})):
    r"""ARCHIVE tab — chat Q&A over the Event Drive. Three paths:

    presence/listing → all names + links (cap 30, newest first);
    title search → tokenize + score doc names (year-filtered on a \b20\d{2}\b in
    the question); matches as "• name — link", else 3 closest titles + folder link;
    detail/synthesis → top-K docs by title score, bodies fed to the Q&A LLM
    (source-only gem), graceful-degrade to a fixed message + sources if the gateway
    is down (NEVER HTTP 500)."""
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

    content = _archive_content_tokens(question)  # presence = empty; also feeds title scoring
    # general listing (no specific event named) → all names + links
    if not content:
        top = docs[:30]
        return {
            "answer": "\n".join(f"• {d['name']} — {d['webViewLink']}" for d in top),
            "sources": [{"name": d["name"], "url": d["webViewLink"]} for d in top],
            "mode": "direct",
        }

    # specific lookup → title-search the content tokens (year-filtered). The scored
    # pool feeds BOTH the direct-match return and the synthesis branch, so the
    # scoring runs once. Detail questions (when/where/how much) go to synthesis;
    # plain name lookups return the matched titles directly (no LLM cost).
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

    if not _archive_is_detail(question):
        if matches:
            return {
                "answer": "\n".join(f"• {d['name']} — {d['webViewLink']}" for d in matches),
                "sources": [{"name": d["name"], "url": d["webViewLink"]} for d in matches],
                "mode": "direct",
            }
        # no title match → 3 closest titles + folder link
        sugg = [d["name"] for _, d in scored[:3]]
        answer = f"No events matching `{question}` in the Event Drive."
        if sugg:
            answer += "\n\nClosest titles:\n" + "\n".join(f"• {n}" for n in sugg)
        answer += f"\n\nBrowse the Event Drive: {folder_link}"
        return {"answer": answer, "sources": [], "mode": "direct"}

    # --- detail / synthesis (Stage 2): feed the top docs' bodies to the Q&A LLM ---
    # Top-5 by title score. With real matches the LLM answers from their bodies; with
    # zero score>0 it gets the 5 closest anyway so it can honestly say "does not list".
    matched_docs = matches[:5] if matches else [d for _, d in scored[:5]]
    bodies: list[str] = []
    for d in matched_docs[:5]:
        body = await _drive_read_doc(token, d["id"])
        if len(body) > 8000:
            body = body[:8000] + "\n[...truncated]"
        bodies.append(body)
    top = matched_docs[:5]
    system = _load_gem(_archive_gem_path())
    model = (_opts().get("archive_llm") or {}).get("model") or "glm-5"
    prompt = (
        "Question: " + question + "\n\nEvent docs:\n"
        + "\n\n".join("=== DOC: " + d["name"] + " ===\n" + b for d, b in zip(top, bodies))
    )
    try:
        answer = await zai_message(
            prompt, max_tokens=4096, system=system, model=model, timeout=180
        )
        return {
            "answer": answer,
            "sources": [
                {"name": d["name"], "url": d["webViewLink"]}
                for d in top
            ],
            "mode": "synthesized",
        }
    except HTTPException:
        # Gateway 503/502 → degrade to a fixed message + the candidate sources.
        # NEVER let a gateway failure surface as HTTP 500.
        return {
            "answer": "Found " + str(len(matched_docs)) + " candidate event doc(s) in "
                      "the Event Drive; Q&A synthesis is unavailable right now (the LLM "
                      "gateway is down). See the sources below.",
            "sources": [{"name": d["name"], "url": d["webViewLink"]} for d in matched_docs],
            "mode": "degraded",
        }


# --- SEO sub-module (HEALTH: read-only link/image/orphan report) ----------------
# Authed WP REST (Basic auth, app password). The Sucuri WAF 403s *unauthenticated*
# fetches but authed REST passes (verified). Creds in /home/NAZ/n8n/.secrets.env.
# Phase 1 = detect-only report (this block); the fix (bulk-unlink / suggest-insert)
# is Phase 2, later. Internal links/images are validated against the fetched
# post/page/media sets (no fetching of thailandnow.in.th pages → Sucuri-proof);
# only external links/images need real HTTP checks (S2).

# WP-generated archive/asset paths — an internal href into one of these is NOT a
# broken post link even though it isn't a post/page URL.
_WP_ARCHIVE_PREFIXES = (
    "/category/", "/tag/", "/author/", "/page/", "/feed/", "/wp-content/",
    "/wp-admin/", "/wp-json/", "/wp-login", "/wp-includes/", "/?p=", "/?page_id=",
    "/?cat=", "/?tag=", "/?author=",
)
_SEO_MEDIA_CACHE: tuple[set[str], float] | None = None  # (source_urls, fetched_epoch)
_SEO_STOP = {  # generic + site words, stripped for orphan-suggestion token overlap
    "the", "a", "an", "of", "in", "on", "for", "and", "or", "to", "with", "at",
    "by", "from", "is", "are", "was", "were", "be", "as", "it", "its", "that",
    "this", "thailand", "thai", "now", "news", "how", "what", "when", "where",
}


def _wp_creds() -> tuple[str, str, str]:
    """(base_url, user, app-password). Checks ~/.config/railjack/wp.json first,
    then options.wordpress_url, then WORDPRESS_URL/USERNAME/APPLICATION_PASSWORD
    in env or secrets files. HTTPException(503) if incomplete."""
    wp_json_path = Path.home() / ".config" / "railjack" / "wp.json"
    if wp_json_path.is_file():
        try:
            data = json.loads(wp_json_path.read_text())
            url = (data.get("url") or data.get("wordpress_url") or "").rstrip("/")
            user = data.get("username") or data.get("user") or data.get("wordpress_username") or ""
            pwd = (
                data.get("application_password")
                or data.get("password")
                or data.get("wordpress_application_password")
                or data.get("wordpress_password")
                or data.get("app_password")
                or ""
            )
            if url and user and pwd:
                return str(url).strip().rstrip("/"), str(user).strip(), str(pwd).strip()
        except Exception:
            pass

    opts = _opts()
    url = (opts.get("wordpress_url") or _secret("WORDPRESS_URL") or "").rstrip("/")
    user = _secret("WORDPRESS_USERNAME") or ""
    pwd = (
        _secret("WORDPRESS_APPLICATION_PASSWORD")
        or _secret("WORDPRESS_PASSWORD")
        or _secret("WORDPRESS_APP_PASSWORD")
        or ""
    )
    if not (url and user and pwd):
        raise HTTPException(
            503,
            "WordPress creds not configured (WORDPRESS_URL/USERNAME/APPLICATION_PASSWORD "
            "in env, ~/.config/railjack/wp.json, or /home/NAZ/n8n/.secrets.env)",
        )
    return url, user, pwd


def _wp_site_host() -> str:
    """Bare host (no leading www.) of the WP site, for internal-link classification."""
    h = urllib.parse.urlparse(_wp_creds()[0]).netloc
    return h[4:] if h.startswith("www.") else h


async def _wp(method: str, path: str, params: dict | None = None, json_body: dict | None = None):
    """Authed WP REST call (Basic auth via httpx). Returns parsed JSON or dict/None."""
    url, user, pwd = _wp_creds()
    async with httpx.AsyncClient(timeout=30, auth=(user, pwd), follow_redirects=True) as c:
        r = await c.request(method, f"{url}/wp-json/wp/v2{path}", params=params or {}, json=json_body)
        if r.status_code >= 400:
            raise HTTPException(502, f"WP {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json() if r.content else {}


_WP_RB_CACHE: dict[int, str] = {}           # post_id -> rest_base (process-lifetime)
_WP_CONTENT_RBS: tuple[str, ...] | None = None


async def _wp_content_rest_bases() -> tuple[str, ...]:
    """Content-bearing WP REST bases (posts, pages, event, + public CPTs). Cached.

    The SEO fix endpoints need this: a record id (e.g. an event) is NOT reachable
    via /posts/{id} — WP REST has no cross-type lookup-by-id, so we resolve by
    probing each base. ponytail: process cache; types rarely change, restart refreshes."""
    global _WP_CONTENT_RBS
    if _WP_CONTENT_RBS:
        return _WP_CONTENT_RBS
    bases = ["posts", "pages"]
    _skip = {"attachment", "revision", "nav_menu_item", "wp_block",
             "wp_template", "wp_template_part", "wp_navigation", "user"}
    try:
        types_obj = await _wp("GET", "/types", {})
        if isinstance(types_obj, dict):
            for slug, info in types_obj.items():
                rb = (info or {}).get("rest_base") if isinstance(info, dict) else None
                if rb and "{" not in rb and slug not in _skip and rb not in bases:
                    bases.append(rb)
    except HTTPException:
        pass
    _WP_CONTENT_RBS = tuple(bases)
    return _WP_CONTENT_RBS


async def _wp_resolve_rest_base(post_id: int) -> str:
    """Which content rest_base owns post_id? Cached per-id; falls back to 'posts'
    (the caller then surfaces an honest 404). Probes with context=view (read perm)."""
    if post_id in _WP_RB_CACHE:
        return _WP_RB_CACHE[post_id]
    for rb in await _wp_content_rest_bases():
        try:
            if await _wp("GET", f"/{rb}/{post_id}", {"context": "view"}):
                _WP_RB_CACHE[post_id] = rb
                return rb
        except HTTPException:
            continue
    return "posts"


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


async def _wp_list_all(endpoint: str, fields: str) -> list[dict]:
    """Page through a WP REST collection at per_page=100 (?page=N). Stops on a
    short/empty page; hard cap 200 pages."""
    out: list[dict] = []
    for page in range(1, 201):
        batch = await _wp("GET", endpoint, {"per_page": 100, "page": page, "_fields": fields})
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


async def _seo_media_set() -> set[str]:
    """Cached set of every image URL WP knows (TTL options.seo_media_cache_ttl_s,
    default 24h): each item's ``source_url`` PLUS every ``media_details.sizes[*]
    .source_url`` (the generated crops). ~5750 items — pulled once, reused across
    scans. Module-global cache. Including crops at match time kills the bulk of
    image false-positives (content refs sized crops, not the original)."""
    global _SEO_MEDIA_CACHE
    ttl = float(_opts().get("seo_media_cache_ttl_s", 86400))
    now = time.time()
    if _SEO_MEDIA_CACHE and (now - _SEO_MEDIA_CACHE[1]) < ttl:
        return _SEO_MEDIA_CACHE[0]
    items = await _wp_list_all("/media", "id,source_url,media_details")
    urls: set[str] = set()
    for it in items:
        if it.get("source_url"):
            urls.add(it["source_url"])
        sizes = (it.get("media_details") or {}).get("sizes") or {}
        for s in sizes.values():
            if s.get("source_url"):
                urls.add(s["source_url"])
    _SEO_MEDIA_CACHE = (urls, now)
    return urls


class _SeoLinkImgParser(HTMLParser):
    """Collect ``<a href>`` and ``<img>`` source URLs from rendered WP content,
    skipping ``data:``/``blob:`` lazy-load placeholders. Captures src +
    data-src/data-lazy-src + srcset so the *real* (non-placeholder) image URL is
    recorded even when ``src`` is a 1x1 gif."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.imgs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            h = a.get("href")
            if h:
                self.links.append(h)
        elif tag == "img":
            seen: set[str] = set()
            for k in ("data-src", "data-lazy-src", "data-lazy", "src"):
                v = (a.get(k) or "").strip()
                if v and not v.startswith(("data:", "blob:")) and v not in seen:
                    self.imgs.append(v)
                    seen.add(v)
            for k in ("srcset", "data-srcset"):
                ss = a.get(k)
                if not ss:
                    continue
                for cand in ss.split(","):
                    url = cand.strip().split(" ")[0]
                    if url and not url.startswith(("data:", "blob:")) and url not in seen:
                        self.imgs.append(url)
                        seen.add(url)


def _seo_host_eq(a: str, b: str) -> bool:
    a = a[4:] if a.startswith("www.") else a
    b = b[4:] if b.startswith("www.") else b
    return a == b


def _seo_classify(url: str, site_host: str) -> str | None:
    """'internal' | 'external' | None (skip anchors / mailto / tel / javascript /
    data / blob). Site-relative ("/path", "//host/path", bare "foo.html") → internal."""
    u = (url or "").strip()
    if not u or u[0] == "#" or u.startswith(("mailto:", "tel:", "javascript:", "data:", "blob:")):
        return None
    if u.startswith("//"):
        host = urllib.parse.urlparse("https:" + u).netloc
    elif u.startswith("/"):
        return "internal"
    else:
        host = urllib.parse.urlparse(u).netloc
        if not host:  # bare relative ("foo.html") — treat as site-relative
            return "internal"
    return "internal" if _seo_host_eq(host, site_host) else "external"


def _seo_path(url: str) -> str:
    """Path component (no query/fragment), trailing slash trimmed, for matching."""
    return urllib.parse.urlparse(url).path.rstrip("/") or "/"


def _seo_extract(html: str, site_host: str) -> dict:
    """Parse rendered WP HTML → deduped {internal_links, external_links,
    internal_imgs, external_imgs}. Pure (no network)."""
    p = _SeoLinkImgParser()
    try:
        p.feed(html or "")
    except Exception:  # malformed HTML — keep what we parsed
        pass
    out = {"internal_links": [], "external_links": [], "internal_imgs": [], "external_imgs": []}
    seen_l: set[str] = set()
    for h in p.links:
        c = _seo_classify(h, site_host)
        if c in ("internal", "external") and h not in seen_l:
            out[c + "_links"].append(h)
            seen_l.add(h)
    seen_i: set[str] = set()
    for s in p.imgs:
        c = _seo_classify(s, site_host)
        if c in ("internal", "external") and s not in seen_i:
            out[c + "_imgs"].append(s)
            seen_i.add(s)
    return out


def _seo_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if len(w) > 2 and w not in _SEO_STOP}


def _seo_suggest(orphan_title: str, candidates: list[tuple[str, str]], n: int = 3) -> list[dict]:
    """Top-n ``{link,title}`` by title-token overlap with the orphan. ``candidates``
    is (link, title) pairs excluding the orphan itself. Pure + deterministic."""
    ot = _seo_tokens(orphan_title)
    scored: list[tuple[int, str, str]] = []
    for link, title in candidates:
        if not link:
            continue
        ov = len(ot & _seo_tokens(title))
        if ov > 0:
            scored.append((ov, link, title))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"link": l, "title": t} for _, l, t in scored[:n]]


def _seo_img_base(url: str) -> str:
    """Normalise a WP image URL to its library original so a content ref matches:
    strip a size suffix (``-WxH``) AND an edit-variant suffix (``-e{timestamp}``)
    before the extension. '.../khon-09-1024x683.jpg' -> '.../khon-09.jpg';
    '.../khon-09-e1596123456.jpg' -> '.../khon-09.jpg'."""
    url = re.sub(r"-\d+x\d+(?=\.[a-zA-Z]{2,4}$)", "", url)
    return re.sub(r"-e\d+(?=\.[a-zA-Z]{2,4}$)", "", url)


def _seo_img_has_ext(url: str) -> bool:
    """True if the URL ends in a recognisable image extension (else it's an
    unvalidatable edit/placeholder ref — skip rather than false-flag)."""
    return bool(re.search(r"\.(jpe?g|png|webp|gif|svg|avif)(?:$|\?)", url, re.I))


_MANGLED_HOST_RE = re.compile(
    r"(?:https?:)?//(?:www\.)?[a-z0-9-]+\.(?:com|net|org|co|io|gov|me|th)|"
    r"(?:www\.)?[a-z0-9-]+\.(?:com|net|org|co|io|gov|me|th)|"
    r"(?:facebook|fb|twitter|x|instagram|ig|youtube|yt|tiktok|linkedin|t\.me|lineblog|pinterest|reddit)",
    re.IGNORECASE,
)


async def _seo_fetch_all() -> tuple[list[dict], list[dict], list[dict], list[dict], set[str], set[str]]:
    """Fetch content-bearing types (posts + pages + event CPT + all public CPTs, full content)
    + the media set + category/tag archive paths. Content types feed parsing; their
    paths plus the taxonomy paths form the valid-internal-path set. Soft on the
    optional types — event/cpts/categories/tags that aren't REST-enabled are skipped."""
    posts = await _wp_list_all("/posts", "id,link,slug,title,content")
    pages = await _wp_list_all("/pages", "id,link,slug,title,content")
    try:
        events = await _wp_list_all("/event", "id,link,slug,title,content")
    except HTTPException:
        events = []

    # S1.1: Discover and fetch all public custom post types (CPTs) like /interviews
    other_cpts: list[dict] = []
    _skip_types = {
        "post", "page", "event", "attachment", "revision", "nav_menu_item",
        "wp_block", "wp_template", "wp_template_part", "wp_navigation", "user"
    }
    try:
        types_obj = await _wp("GET", "/types", {})
        if isinstance(types_obj, dict):
            for slug, info in types_obj.items():
                if not isinstance(info, dict):
                    continue
                rest_base = info.get("rest_base")
                # Fix 3: public /types doesn't expose show_in_rest; gate on
                # rest_base presence only (+ skip builtins).
                if rest_base and slug not in _skip_types:
                    try:
                        cpt_items = await _wp_list_all(f"/{rest_base}", "id,link,slug,title,content")
                        other_cpts.extend(cpt_items)
                    except HTTPException:
                        pass
    except HTTPException:
        pass

    media = await _seo_media_set()
    extra: set[str] = set()
    for ep in ("/categories", "/tags"):
        try:
            terms = await _wp_list_all(ep, "id,link")
        except HTTPException:
            continue
        for t in terms:
            if t.get("link"):
                extra.add(_seo_path(t["link"]))
    return posts, pages, events, other_cpts, media, extra


def _seo_internal_report(
    posts: list[dict], pages: list[dict], events: list[dict], other_cpts: list[dict] | None,
    media_urls: set[str], extra_paths: set[str], site_host: str,
) -> dict:
    """Pure: from content-bearing WP records (posts + pages + event CPT + other CPTs; each with
    ``id``, ``link``, ``title.rendered``, ``content.rendered``) + the media URL set
    (originals + crops) + extra valid paths (category/tag archives), compute broken
    internal links, internal image candidates (src ∉ media set — HTTP-probed later
    by the flow), orphan posts/events/CPTs, and the external link/image sets. No network.
    Image crops/edit-variants are matched to their media original by stripping the
    -WxH / -e{ts} suffix; extensionless refs are skipped."""
    media_bases = {_seo_img_base(m) for m in media_urls}
    other_cpts = other_cpts or []

    def parse(rec: dict) -> dict:
        html = (rec.get("content") or {}).get("rendered", "")
        ex = _seo_extract(html, site_host)
        return {
            "id": rec.get("id"),
            "link": rec.get("link", ""),
            "title": (rec.get("title") or {}).get("rendered", ""),
            "path": _seo_path(rec.get("link", "")),
            "internal_link_pairs": [(_seo_path(u), u) for u in ex["internal_links"]],
            "internal_imgs": ex["internal_imgs"],
            "external_links": ex["external_links"],
            "external_imgs": ex["external_imgs"],
        }

    p_items = [parse(r) for r in posts]
    e_items = [parse(r) for r in events]
    g_items = [parse(r) for r in pages]
    o_items = [parse(r) for r in other_cpts]
    article_items = p_items + e_items + o_items   # orphan-eligible (posts, events, and extra CPTs)
    all_items = article_items + g_items          # everything that bears links
    valid_paths = {it["path"] for it in all_items if it["link"]} | set(extra_paths)

    # S1.3: Per-post attribution for external links and images
    ext_sources: dict[str, list[dict]] = {}
    for it in all_items:
        rec_info = {"from": it["link"], "from_id": it["id"], "from_title": it["title"]}
        for ext_u in (it["external_links"] + it["external_imgs"]):
            if ext_u not in ext_sources:
                ext_sources[ext_u] = []
            if not any(s["from"] == it["link"] for s in ext_sources[ext_u]):
                ext_sources[ext_u].append(rec_info)

    # Fix 1: emit candidates for HTTP verification, not final broken_internal_links.
    # The flow will probe each and classify as broken / manual / drop.
    internal_link_candidates: list[dict] = []
    for it in all_items:
        for tgt, raw_href in it["internal_link_pairs"]:
            if tgt in valid_paths or tgt == it["path"]:
                continue
            if any(tgt.startswith(pfx) for pfx in _WP_ARCHIVE_PREFIXES):
                continue
            internal_link_candidates.append({
                "from": it["link"], "from_id": it["id"], "from_title": it["title"],
                "to": tgt, "href": raw_href,
            })

    image_candidates: list[dict] = []  # src ∉ media set → HTTP-probed in _flow_seo_health
    for it in all_items:
        for src in it["internal_imgs"]:
            if not _seo_img_has_ext(src):
                continue  # extensionless edit/placeholder ref — unvalidatable, skip
            if _seo_img_base(src) not in media_bases:
                image_candidates.append(
                    {"from": it["link"], "from_id": it["id"], "from_title": it["title"], "src": src})

    # orphan ARTICLES (posts + events + CPTs): zero inbound internal links from anywhere
    inbound = {it["path"]: 0 for it in article_items}
    for src_it in all_items:
        for tgt, _ in src_it["internal_link_pairs"]:
            if tgt != src_it["path"] and tgt in inbound:
                inbound[tgt] += 1
    orphans = [{"id": it["id"], "link": it["link"], "title": it["title"]}
               for it in article_items if inbound.get(it["path"], 0) == 0]

    return {
        "post_count": len(posts),
        "page_count": len(pages),
        "event_count": len(events),
        "other_cpt_count": len(other_cpts),
        "valid_paths": len(valid_paths),
        "internal_link_candidates": internal_link_candidates,  # HTTP-verified in _flow_seo_health
        "image_candidates": image_candidates,   # → broken_internal_images / image_manual_check in the flow
        "orphans": orphans,
        "external_links": sorted({u for it in all_items for u in it["external_links"]}),
        "external_imgs": sorted({u for it in all_items for u in it["external_imgs"]}),
        "ext_sources": ext_sources,
    }


def _seo_resolve_href(raw_href: str, site_origin: str) -> str:
    """Resolve a raw internal-link href to an absolute URL for probing.
    Bare relative ('Facebook/X', '/foo') → site_origin + '/' + path;
    schemeless ('//host/...') → 'https://host/...'; already-absolute → as-is.
    Strips leading whitespace/junk text before resolving (e.g. 'Source: Facebook/X'
    → site_origin + '/Source: Facebook/X' → will 404, which is correct)."""
    h = (raw_href or "").strip()
    if not h:
        return ""
    if h.startswith("//"):
        return "https:" + h
    parsed = urllib.parse.urlparse(h)
    if parsed.scheme in ("http", "https"):
        return h
    # Bare relative — resolve against the site origin
    if h.startswith("/"):
        return site_origin + h
    return site_origin + "/" + h


def _seo_internal_link_reason(raw_href: str, site_host: str) -> str:
    """Classify WHY a confirmed-broken internal link is broken (for the UI).
    Fix 2: _MANGLED_HOST_RE now has no ^ anchors, so 'Source: Facebook/...' matches.
    Exclude the site's own host from the mangled-external label."""
    raw_clean = (raw_href or "").strip()
    if not raw_clean or raw_clean == "#":
        return "empty/anchor-only"
    # Check if the href resolves to the site's own host → internal, not mangled-external
    try:
        parsed_host = urllib.parse.urlparse(raw_clean).netloc
        if parsed_host and _seo_host_eq(parsed_host, site_host):
            return "not a published page"
    except Exception:
        pass
    if _MANGLED_HOST_RE.search(raw_clean.lstrip("/")):
        return "likely a broken external link (missing scheme)"
    return "not a published page"


async def _flow_seo_health(job: "TnJob") -> None:
    """HEALTH scan: fetch WP content → internal report → orphan suggestions →
    external HTTP checks → assemble the full report into ``job.result``. Honours
    ``job.cancel`` between phases + between external-check batches (raises
    _TnCancelled). Rides the shared TnJob store (kind=seo-health)."""
    job.progress = 5
    posts, pages, events, other_cpts, media, extra = await _seo_fetch_all()
    if job.cancel:
        raise _TnCancelled()
    job.progress = 25
    rep = _seo_internal_report(posts, pages, events, other_cpts, media, extra, _wp_site_host())
    job.progress = 40
    # orphan suggestions: top-3 by title-token overlap (exclude the orphan itself)
    titles = [(p.get("link", ""), (p.get("title") or {}).get("rendered", ""))
              for p in (posts + events + other_cpts) if p.get("link")]
    link_ids = {p.get("link", ""): p.get("id") for p in (posts + events + other_cpts)}
    for o in rep["orphans"]:
        cands = [(l, t) for (l, t) in titles if l != o["link"]]
        o["suggested"] = _seo_suggest(o["title"], cands, n=3)
        for s in o["suggested"]:  # host id → anchor ANALYZE endpoint
            s["id"] = link_ids.get(s["link"])
    job.progress = 50
    # HTTP-verify external links/images, internal image candidates, AND internal
    # link candidates (Fix 1). Internal links use authed probe (Sucuri bypass).
    ext = sorted(set(rep["external_links"]) | set(rep["external_imgs"]))
    img_cands = rep.pop("image_candidates", [])  # intermediate — not in the final report
    int_link_cands = rep.pop("internal_link_candidates", [])  # Fix 1: HTTP-verify
    ext_sources = rep.pop("ext_sources", {})
    conc = max(1, int(_opts().get("seo_external_concurrency", 8)))
    timeout = float(_opts().get("seo_external_timeout_s", 10))
    sem = asyncio.Semaphore(conc)
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    probe_headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
    }
    broken_external: list[dict] = []
    manual_check: list[dict] = []
    broken_images: list[dict] = []          # HTTP-confirmed missing internal images
    image_manual: list[dict] = []
    broken_internal: list[dict] = []        # Fix 1: HTTP-confirmed broken internal links
    internal_manual: list[dict] = []        # Fix 1: internal links that need manual check

    site_host = _wp_site_host()
    site_origin = _wp_creds()[0].rstrip("/")  # e.g. "https://www.thailandnow.in.th"

    async def probe(url: str) -> int:
        if url.startswith("//"):
            url = "https:" + url  # protocol-relative '//host/…' → fetchable
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.head(url, headers=probe_headers)
            if r.status_code == 405:  # some hosts reject HEAD → fall back to GET
                r = await c.get(url, headers=probe_headers)
            return r.status_code

    async def check_ext(url: str) -> None:
        srcs = ext_sources.get(url, [])
        async with sem:
            try:
                code = await probe(url)
            except (httpx.HTTPError, OSError):
                manual_check.append({"url": url, "reason": "timeout/error", "from": srcs})
                return
        if code in (404, 410):
            broken_external.append({"url": url, "status": code, "from": srcs})
        elif code >= 400:
            manual_check.append({"url": url, "status": code, "reason": "client-error/blocked", "from": srcs})

    async def check_img(cand: dict) -> None:
        async with sem:
            try:
                code = await probe(cand["src"])
            except (httpx.HTTPError, OSError):
                image_manual.append({**cand, "reason": "timeout/error"})
                return
        if code in (404, 410):
            broken_images.append({**cand, "status": code})
        elif code >= 400:  # 403/blocked → likely Sucuri on a non-asset path; verify by hand
            image_manual.append({**cand, "status": code, "reason": "client-error/blocked"})
        # 2xx/3xx → image exists (crop/variant the media set didn't list) → drop

    async def check_internal_link(cand: dict) -> None:
        """Fix 1: HTTP-verify an internal link candidate (unauthed, clean UA)."""
        abs_url = _seo_resolve_href(cand["href"], site_origin)
        if not abs_url:
            return  # empty href — skip
        async with sem:
            try:
                code = await probe(abs_url)
            except (httpx.HTTPError, OSError):
                internal_manual.append({
                    **cand,
                    "reason": _seo_internal_link_reason(cand["href"], site_host),
                })
                return
        if code in (404, 410):
            broken_internal.append({
                **cand,
                "reason": _seo_internal_link_reason(cand["href"], site_host),
            })
        elif code >= 400:
            internal_manual.append({
                **cand,
                "reason": _seo_internal_link_reason(cand["href"], site_host),
            })
        # 200/3xx → page exists (theme route, etc.) → drop (e.g. /interviews)

    tasks = [asyncio.create_task(check_ext(u)) for u in ext]
    tasks += [asyncio.create_task(check_img(c)) for c in img_cands]
    tasks += [asyncio.create_task(check_internal_link(c)) for c in int_link_cands]
    total = len(tasks)
    if total:
        for i, fut in enumerate(asyncio.as_completed(tasks), 1):
            await fut
            if i % conc == 0:
                job.progress = min(90, 50 + int(40 * i / total))
                if job.cancel:
                    for t in tasks:
                        t.cancel()
                    raise _TnCancelled()
    job.progress = 95
    job.result = {
        **rep,
        "broken_internal_links": broken_internal,
        "internal_manual_check": internal_manual,
        "broken_internal_images": broken_images,
        "image_manual_check": image_manual,
        "broken_external_links": broken_external,
        "manual_check": manual_check,
        "external_checked": len(ext),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def _seo_norm_url(u: str) -> str:
    """Normalize a URL for matching: strip scheme, www, query, and trailing slashes."""
    u = (u or "").strip().lower()
    u = re.sub(r"^(?:https?:)?//", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?")[0].split("#")[0].rstrip("/")
    return u


def _seo_strip_target(html: str, kind: str, target: str) -> tuple[str, int, str, str]:
    """Pure: find matching <a> or <img> tags in html referencing target, strip them,
    and return (new_html, match_count, snippet_before, snippet_after).
    For <a>: tag stripped, inner text kept as prose.
    For <img>: tag dropped entirely.
    snippets show ~40 chars context around the first match before and after stripping."""
    if not html or not target:
        return html, 0, "", ""

    target_raw = target.strip()
    target_norm = _seo_norm_url(target_raw)
    if not target_norm:
        return html, 0, "", ""

    matches = 0
    first_start = -1
    first_end = -1
    first_replacement = ""

    if kind == "image":
        img_re = re.compile(r"<img\b[^>]*?>", re.IGNORECASE | re.DOTALL)
        pos = 0
        chunks = []
        for m in img_re.finditer(html):
            tag_str = m.group(0)
            tag_norm = _seo_norm_url(tag_str)
            if target_raw in tag_str or (target_norm and target_norm in tag_norm):
                matches += 1
                if first_start == -1:
                    first_start = m.start()
                    first_end = m.end()
                    first_replacement = ""
                chunks.append(html[pos:m.start()])
                pos = m.end()
        chunks.append(html[pos:])
        new_html = "".join(chunks) if matches > 0 else html
    else:  # "link"
        a_re = re.compile(r'<a\b[^>]*?\bhref=["\']?([^"\'>]+)["\']?[^>]*?>(.*?)</a>', re.IGNORECASE | re.DOTALL)
        pos = 0
        chunks = []
        for m in a_re.finditer(html):
            href_val = m.group(1)
            inner_text = m.group(2)
            href_norm = _seo_norm_url(href_val)
            if (target_raw in href_val or href_val in target_raw or
                    (target_norm and target_norm in href_norm)):
                matches += 1
                if first_start == -1:
                    first_start = m.start()
                    first_end = m.end()
                    first_replacement = inner_text
                chunks.append(html[pos:m.start()])
                chunks.append(inner_text)
                pos = m.end()
        chunks.append(html[pos:])
        new_html = "".join(chunks) if matches > 0 else html

    if matches == 0:
        return html, 0, "", ""

    ctx_start = max(0, first_start - 40)
    ctx_end = min(len(html), first_end + 40)
    snippet_before = html[ctx_start:ctx_end]
    snippet_after = html[ctx_start:first_start] + first_replacement + html[first_end:ctx_end]

    return new_html, matches, snippet_before, snippet_after


# regions invalid for anchor insertion: inside <a>/<h1-6>/<pre>/<code> (flat scan —
# no nesting paranoia; enough for WP content)
_SEO_INVALID_REGION_RE = re.compile(
    r"<a\b.*?</a\s*>|<h[1-6]\b.*?</h[1-6]\s*>|<pre\b.*?</pre\s*>|<code\b.*?</code\s*>",
    re.IGNORECASE | re.DOTALL,
)
# any complete tag — its INTERIOR (attributes) is never a valid anchor spot
# (e.g. alt="Khon Kaen Street Food" must not be wrapped); quoted segments may
# contain a literal ">" so skip them when scanning for the tag end
_SEO_ANY_TAG_RE = re.compile(r"<[a-zA-Z/!?](?:\"[^\"]*\"|'[^']*'|[^>])*>")


def _seo_valid_regions(html: str) -> list[tuple[int, int]]:
    """Pure: index ranges of html outside <a>/<h1-6>/<pre>/<code> content AND
    outside every tag interior (attributes are not prose). Case-insensitive,
    DOTALL. ponytail: a single unclosed <a>/<pre> makes its span scan run to
    EOF (O(n) per unclosed tag, so O(n^2) worst case on corrupted HTML) —
    fine for WP-sized posts; upgrade path = a real HTML tokenizer."""
    if not html:
        return []
    spans = [m.span() for m in _SEO_INVALID_REGION_RE.finditer(html)]
    spans += [m.span() for m in _SEO_ANY_TAG_RE.finditer(html)]
    spans.sort()
    out: list[tuple[int, int]] = []
    pos = 0
    for s, e in spans:
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < len(html):
        out.append((pos, len(html)))
    return out


def _seo_anchor_phrases(orphan_title: str) -> list[str]:
    """Pure: candidate anchor phrases from the orphan title — the full
    whitespace-normalized title plus every contiguous token n-gram of length
    >= 2 (Thai titles are one long token, so the full title covers Thai).
    Drops stopword-only phrases (per _SEO_STOP; phrases with no latin tokens,
    i.e. Thai, are kept) and phrases shorter than 4 chars. Deduped, longest-first."""
    title = " ".join((orphan_title or "").split())
    if not title:
        return []
    tokens = title.split()
    phrases = [title] + [
        " ".join(tokens[i:i + n])
        for n in range(2, len(tokens) + 1)
        for i in range(0, len(tokens) - n + 1)
    ]
    seen: set[str] = set()
    out: list[str] = []
    for p in phrases:
        toks = re.findall(r"[a-z0-9]+", p.lower())
        if toks and all(t in _SEO_STOP for t in toks):
            continue
        if len(p) < 4 or p in seen:
            continue
        seen.add(p)
        out.append(p)
    out.sort(key=len, reverse=True)
    return out


def _seo_anchor_candidates(orphan_title: str, host_html: str, cap: int = 8) -> list[dict]:
    """Pure: phrases from _seo_anchor_phrases with >=1 case-insensitive occurrence
    at a valid position in host_html. Returns [{"phrase", "count", "snippet"}]
    (~60-char context around the first valid occurrence), longest-first, capped."""
    regions = _seo_valid_regions(host_html)
    out: list[dict] = []
    for phrase in _seo_anchor_phrases(orphan_title):
        # offsets come from re on the ORIGINAL region text: no lower() index
        # drift (some chars change length when lowered) and non-overlapping
        # counts by finditer's default advance
        pat = re.compile(re.escape(phrase), re.IGNORECASE)
        count = 0
        first = -1
        for start, end in regions:
            for m in pat.finditer(host_html, start, end):
                if first == -1:
                    first = m.start()
                count += 1
        if count == 0:
            continue
        snippet = host_html[max(0, first - 30):min(len(host_html), first + len(phrase) + 30)]
        out.append({"phrase": phrase, "count": count, "snippet": snippet})
        if len(out) >= cap:
            break
    return out


def _seo_insert_link(html: str, phrase: str, href: str) -> tuple[str, int, str, str]:
    """Pure, mirrors _seo_strip_target: wrap the FIRST case-insensitive occurrence
    of phrase at a valid position as <a href="{href}">{original_text}</a> (original
    casing preserved). Returns (new_html, matches, snippet_before, snippet_after)
    with ~40-char context around the match. 0 valid occurrences -> (html, 0, "", "")."""
    if not html or not phrase:
        return html, 0, "", ""
    pat = re.compile(re.escape(phrase), re.IGNORECASE)  # offsets on original html
    for start, end in _seo_valid_regions(html):
        m = pat.search(html, start, end)
        if not m:
            continue
        s, e = m.start(), m.end()
        wrapped = f'<a href="{href}">{html[s:e]}</a>'
        new_html = html[:s] + wrapped + html[e:]
        ctx_s = max(0, s - 40)
        ctx_e = min(len(html), e + 40)
        return (
            new_html,
            1,
            html[ctx_s:ctx_e],
            html[ctx_s:s] + wrapped + html[e:ctx_e],
        )
    return html, 0, "", ""


class SeoFixReq(BaseModel):
    post_id: int
    kind: str  # "link" | "image"
    target: str


class SeoApplyFixBulkReq(BaseModel):
    items: list[SeoFixReq]


class SeoAnchorAnalyzeReq(BaseModel):
    host_id: int
    orphan_title: str
    orphan_link: str


class SeoInsertReq(BaseModel):
    host_id: int
    phrase: str
    href: str


@router.post("/api/thailandnow/seo/scan")
async def seo_scan():
    """HEALTH scan — kick off an async ``seo-health`` job. 409 if one is already
    running (single-flight). Returns ``{id}``; poll ``/api/thailandnow/jobs`` +
    fetch the report via ``/api/thailandnow/seo/report/{id}`` when done."""
    if any(j.kind == "seo-health" and j.status in _TN_RUNNING for j in _TN_JOBS.values()):
        raise HTTPException(409, "an SEO HEALTH scan is already running")
    return _tn_spawn("seo-health", "SEO HEALTH scan", _flow_seo_health)


@router.get("/api/thailandnow/seo/report/{jid}")
async def seo_report(jid: str):
    """Finished HEALTH report for a job (404 no such job; 409 not done yet)."""
    job = _TN_JOBS.get(jid)
    if not job or job.kind != "seo-health":
        raise HTTPException(404, "no such SEO HEALTH job")
    if job.status != "done":
        raise HTTPException(409, f"job is {job.status}; not ready")
    return job.result


@router.post("/api/thailandnow/seo/preview-fix")
async def seo_preview_fix(req: SeoFixReq):
    """Preview removing a broken link or image from a WP post.
    Reads content.raw via GET /wp/v2/posts/{post_id}?context=edit.
    Does NOT modify WP. Returns {post_id, matches, before, after}."""
    rb = await _wp_resolve_rest_base(req.post_id)
    try:
        post = await _wp("GET", f"/{rb}/{req.post_id}", {"context": "edit"})
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise HTTPException(403, f"WP edit context permission denied for post {req.post_id}. Verify WP credentials app-password has edit capabilities.")
        raise
    if not post or not isinstance(post, dict):
        raise HTTPException(404, f"WP {rb[:-1] if rb.endswith('s') else rb} {req.post_id} not found")

    raw_content = (post.get("content") or {}).get("raw", "")
    new_html, matches, before, after = _seo_strip_target(raw_content, req.kind, req.target)
    return {
        "post_id": req.post_id,
        "kind": req.kind,
        "target": req.target,
        "matches": matches,
        "before": before,
        "after": after,
    }


@router.post("/api/thailandnow/seo/apply-fix")
async def seo_apply_fix(req: SeoFixReq):
    """Apply fix: remove broken link or image from WP post.
    Reads content.raw, strips target, POSTs {content: new_raw} to /wp/v2/posts/{post_id}.
    Idempotent: 0 matches -> returns {ok: true, matches: 0}, no PUT/POST."""
    rb = await _wp_resolve_rest_base(req.post_id)
    try:
        post = await _wp("GET", f"/{rb}/{req.post_id}", {"context": "edit"})
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise HTTPException(403, f"WP edit context permission denied for post {req.post_id}.")
        raise
    if not post or not isinstance(post, dict):
        raise HTTPException(404, f"WP {rb[:-1] if rb.endswith('s') else rb} {req.post_id} not found")

    raw_content = (post.get("content") or {}).get("raw", "")
    new_html, matches, before, after = _seo_strip_target(raw_content, req.kind, req.target)

    if matches == 0:
        return {
            "ok": True,
            "matches": 0,
            "post_id": req.post_id,
            "post_link": post.get("link", ""),
        }

    await _wp("POST", f"/{rb}/{req.post_id}", json_body={"content": new_html})
    return {
        "ok": True,
        "matches": matches,
        "post_id": req.post_id,
        "post_link": post.get("link", ""),
    }


@router.post("/api/thailandnow/seo/apply-fix-bulk")
async def seo_apply_fix_bulk(req: SeoApplyFixBulkReq):
    """Bulk apply fix: remove multiple broken links/images.
    Iterates items; continues on error; returns per-item results."""
    results = []
    for item in req.items:
        try:
            res = await seo_apply_fix(item)
            results.append(res)
        except Exception as e:
            results.append({
                "ok": False,
                "error": str(e),
                "post_id": item.post_id,
                "target": item.target,
            })
    removed = len([r for r in results if r.get("ok") and r.get("matches", 0) > 0])
    noop = len([r for r in results if r.get("ok") and r.get("matches", 0) == 0])
    failed = len([r for r in results if not r.get("ok")])
    return {
        "results": results,
        "total": len(req.items),
        "removed": removed,      # actually stripped from WP content
        "noop": noop,            # ok but target not in content (already gone / format mismatch)
        "failed": failed,        # WP error (wrong type 404, perms, etc.)
        "successful": removed,   # honest alias: a real removal (was: any ok, incl. no-ops)
    }


@router.post("/api/thailandnow/seo/analyze-anchors")
async def seo_analyze_anchors(req: SeoAnchorAnalyzeReq):
    """Find anchor candidates in a host article's content for an orphan's title
    (inbound link direction). Validates orphan_link is internal. Reads
    content.raw; does NOT modify WP. Returns {host_id, orphan_title,
    orphan_link, candidates}."""
    if _seo_classify(req.orphan_link, _wp_site_host()) != "internal":
        raise HTTPException(400, "orphan_link is not an internal site link")
    rb = await _wp_resolve_rest_base(req.host_id)
    try:
        post = await _wp("GET", f"/{rb}/{req.host_id}", {"context": "edit"})
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise HTTPException(403, f"WP edit context permission denied for post {req.host_id}. Verify WP credentials app-password has edit capabilities.")
        raise
    if not post or not isinstance(post, dict):
        raise HTTPException(404, f"WP {rb[:-1] if rb.endswith('s') else rb} {req.host_id} not found")

    raw_content = (post.get("content") or {}).get("raw", "")
    return {
        "host_id": req.host_id,
        "orphan_title": req.orphan_title,
        "orphan_link": req.orphan_link,
        "candidates": _seo_anchor_candidates(req.orphan_title, raw_content),
    }


@router.post("/api/thailandnow/seo/preview-insert")
async def seo_preview_insert(req: SeoInsertReq):
    """Preview inserting an inbound <a href> around a phrase in a WP post.
    Validates href is internal. Reads content.raw; does NOT modify WP.
    Returns {host_id, phrase, href, matches, before, after}."""
    if _seo_classify(req.href, _wp_site_host()) != "internal":
        raise HTTPException(400, "href is not an internal site link")
    rb = await _wp_resolve_rest_base(req.host_id)
    try:
        post = await _wp("GET", f"/{rb}/{req.host_id}", {"context": "edit"})
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise HTTPException(403, f"WP edit context permission denied for post {req.host_id}. Verify WP credentials app-password has edit capabilities.")
        raise
    if not post or not isinstance(post, dict):
        raise HTTPException(404, f"WP {rb[:-1] if rb.endswith('s') else rb} {req.host_id} not found")

    raw_content = (post.get("content") or {}).get("raw", "")
    new_html, matches, before, after = _seo_insert_link(raw_content, req.phrase, req.href)
    return {
        "host_id": req.host_id,
        "phrase": req.phrase,
        "href": req.href,
        "matches": matches,
        "before": before,
        "after": after,
    }


@router.post("/api/thailandnow/seo/apply-insert")
async def seo_apply_insert(req: SeoInsertReq):
    """Apply insert: wrap the first valid occurrence of phrase in a WP post with
    <a href="{href}">. Validates href is internal. Idempotent: 0 matches ->
    returns {ok: true, matches: 0} without writing."""
    if _seo_classify(req.href, _wp_site_host()) != "internal":
        raise HTTPException(400, "href is not an internal site link")
    rb = await _wp_resolve_rest_base(req.host_id)
    try:
        post = await _wp("GET", f"/{rb}/{req.host_id}", {"context": "edit"})
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise HTTPException(403, f"WP edit context permission denied for post {req.host_id}.")
        raise
    if not post or not isinstance(post, dict):
        raise HTTPException(404, f"WP {rb[:-1] if rb.endswith('s') else rb} {req.host_id} not found")

    raw_content = (post.get("content") or {}).get("raw", "")
    new_html, matches, _, _ = _seo_insert_link(raw_content, req.phrase, req.href)

    if matches == 0:
        return {
            "ok": True,
            "matches": 0,
            "post_id": req.host_id,
            "post_link": post.get("link", ""),
        }

    await _wp("POST", f"/{rb}/{req.host_id}", json_body={"content": new_html})
    return {
        "ok": True,
        "matches": matches,
        "post_id": req.host_id,
        "post_link": post.get("link", ""),
    }


# --- TRAFFIC sub-module (daily GA4 cumulative totals → Analytics & Boosting sheet)
# Replaces the manual ritual: GA4 Total Users (cumulative since contract start)
# per picked date → diff preview vs the sheet → confirmed apply. Service-account
# auth (RS256 JWT via pyjwt — stdlib has no RSA), separate from _google_token.

_TRAFFIC_SCOPES = ("https://www.googleapis.com/auth/spreadsheets "
                   "https://www.googleapis.com/auth/analytics.readonly")
_GA_CFG_DEFAULTS = {"tab": "2025-26 Web Traffic Report", "contract_start": "2025-12-05"}


def _ga_config() -> dict:
    """GA4 + sheet config for the TRAFFIC tab. ~/.config/railjack/ga.json first,
    then GA_CLIENT_EMAIL/GA_PRIVATE_KEY/GA_PROPERTY_ID/GA_SHEET_ID in env or the
    secrets files (mirrors _wp_creds). HTTPException(503) with a human hint when
    incomplete. The private key is never logged or returned."""
    cfg = dict(_GA_CFG_DEFAULTS)
    ga_json_path = Path.home() / ".config" / "railjack" / "ga.json"
    if ga_json_path.is_file():
        try:
            cfg.update({k: v for k, v in json.loads(ga_json_path.read_text()).items() if v})
        except Exception:
            pass
    cfg["client_email"] = cfg.get("client_email") or _secret("GA_CLIENT_EMAIL") or ""
    cfg["private_key"] = (cfg.get("private_key") or _secret("GA_PRIVATE_KEY") or "")
    cfg["property_id"] = str(cfg.get("property_id") or _secret("GA_PROPERTY_ID") or "")
    cfg["sheet_id"] = cfg.get("sheet_id") or _secret("GA_SHEET_ID") or ""
    missing = [k for k in ("client_email", "private_key", "property_id", "sheet_id") if not cfg[k]]
    if missing:
        raise HTTPException(
            503,
            "GA config incomplete (missing " + ", ".join(missing) + ") — place the "
            "service-account config at ~/.config/railjack/ga.json (client_email, "
            "private_key, property_id, sheet_id, tab, contract_start), or set "
            "GA_CLIENT_EMAIL / GA_PRIVATE_KEY / GA_PROPERTY_ID / GA_SHEET_ID in env "
            "or the railjack secrets files",
        )
    # env vars carry the key with literal \n escapes — pyjwt needs real newlines
    cfg["private_key"] = cfg["private_key"].replace("\\n", "\n")
    return cfg


_SA_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


async def _sa_google_token(scope: str) -> str:
    """Service-account → Google access token for ``scope`` (space-separated list).
    RS256 JWT (iss=client_email, aud=token endpoint) exchanged at oauth2.googleapis.
    Separate from the OAuth ``_google_token`` helper. Last token+exp cached in a
    module global; re-minted only when expired (60s margin)."""
    cached = _SA_TOKEN_CACHE.get(scope)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    cfg = _ga_config()
    now = int(time.time())
    claim = {
        "iss": cfg["client_email"], "scope": scope,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now, "exp": now + 3600,
    }
    assertion = jwt.encode(claim, cfg["private_key"], algorithm="RS256")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": assertion},
        )
        if r.status_code != 200:
            raise HTTPException(502, f"Google SA token exchange failed: {r.status_code} {r.text[:200]}")
        token = r.json()["access_token"]
    _SA_TOKEN_CACHE[scope] = (token, now + 3600)
    return token


def _traffic_day(date_iso: str, contract_start_iso: str) -> int:
    """Days since contract start, 1-based (Dec 5 → 1). Pure."""
    d = datetime.strptime(date_iso[:10], "%Y-%m-%d")
    s = datetime.strptime(contract_start_iso[:10], "%Y-%m-%d")
    return (d - s).days + 1


def _traffic_dates(from_iso: str, to_iso: str) -> list[str]:
    """Inclusive ISO dates. Callers pass Asia/Bangkok dates. ValueError on a
    reversed range or more than 92 dates (keeps the GA query count bounded;
    92 DATES, matching the app's lane and '≤92 dates per run')."""
    d1 = datetime.strptime(from_iso[:10], "%Y-%m-%d")
    d2 = datetime.strptime(to_iso[:10], "%Y-%m-%d")
    span = (d2 - d1).days
    if span < 0:
        raise ValueError(f"from {from_iso} is after to {to_iso}")
    if span > 91:
        raise ValueError(f"range too wide: {span + 1} dates (max 92)")
    return [(d1 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(span + 1)]


def _traffic_text_lines(rows: list[dict]) -> str:
    """One paste-ready line per day:
    ``{Mon D} · Day {n} · Total {t:,} · Daily {+d:,} · Target {target:,} (Δ {±gap:,})``
    Daily 0 → ``+0``; missing target omits the Target and (Δ …) parts. Pure."""
    lines: list[str] = []
    for r in rows or []:
        d = datetime.strptime(r["date"][:10], "%Y-%m-%d")
        line = (f"{d.strftime('%b')} {d.day} · Day {r['day']} · "
                f"Total {r['total']:,} · Daily {(r.get('daily') or 0):+,}")
        t = r.get("target")
        if t is not None:
            line += f" · Target {t:,} (Δ {r['total'] - t:+,})"
        lines.append(line)
    return "\n".join(lines)


def _traffic_cell_int(v) -> int | None:
    """Sheet cell → int (accepts int/float/numeric string with commas); None otherwise."""
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v).strip().replace(",", ""))
    except ValueError:
        return None


_TRAFFIC_SERIAL_EPOCH = datetime(1899, 12, 30)  # sheet serial 0 (1900 date system)


def _traffic_serial_to_iso(serial: int) -> str:
    return (_TRAFFIC_SERIAL_EPOCH + timedelta(days=int(serial))).strftime("%Y-%m-%d")


def _traffic_iso_to_serial(date_iso: str) -> int:
    return (datetime.strptime(date_iso[:10], "%Y-%m-%d") - _TRAFFIC_SERIAL_EPOCH).days


def _traffic_columns(sheet_rows: list[list]) -> tuple[int, dict[str, int]]:
    """(header_row_number_1based, {name: 0-based col index}). The header row is the
    first row within the first ~50 rows containing a trimmed case-insensitive 'Day'
    cell (rows 2-6 hold phase metadata — ignored); column names matched by prefix
    (Date/Day/Target Traffic/Actual Traffic/Daily Traffic — Date lives at B).
    HTTPException(502) when the layout isn't recognized."""
    for i, r in enumerate(sheet_rows or []):
        if i >= 50:
            break
        cells = [str(c).strip().lower() for c in (r or [])]
        if "day" not in cells:
            continue
        cols: dict[str, int] = {}
        for j, c in enumerate(cells):
            if c == "day":
                cols.setdefault("day", j)
            elif c.startswith("date"):
                cols.setdefault("date", j)
            elif c.startswith("target"):
                cols.setdefault("target", j)
            elif c.startswith("actual"):
                cols.setdefault("actual", j)
            elif c.startswith("daily"):
                cols.setdefault("daily", j)
        if {"date", "day", "actual"} <= set(cols):
            return i + 1, cols
    raise HTTPException(
        502, "traffic sheet layout unrecognized — no header row with Date/Day/Target "
             "Traffic/Actual Traffic/Daily Traffic in the first 50 rows",
    )


# late-phase Target/Daily formula shapes (early `=$I$3*C10` targets don't shift — blank+warn)
_TRAFFIC_D_FORMULA_RE = re.compile(r"^=D(\d+)\+(\$[A-Z]\$\d+)$")
_TRAFFIC_F_FORMULA_RE = re.compile(r"^=E(\d+)-E(\d+)$")


def _traffic_proposed_writes(sheet_rows: list[list], dates: list[str], ga: dict[str, int],
                             contract_start_iso: str, header_row: int,
                             columns: dict[str, int]) -> dict:
    """THE CORE (pure). ``sheet_rows`` = raw values of the tab (rows 2-6 are a
    phase-metadata block — ignored; the real header row is found by
    ``_traffic_columns``, its indexes passed here). Layout below the header:
    A empty, B serial date (45996 = 2025-12-05), C Day (1-based), D Target
    (formula), E Actual (plain — the ONLY column we write), F Daily (formula);
    rows may be ragged; future rows exist with D/F prefilled and E empty.

    Row matching: expected Day number (from the date) against the Day column as
    primary key; the serial date as exact sanity check — a row holding that date
    whose Day disagrees → warning + skip. Day-1 anchor sanity: the Day-1 row's
    serial must equal the contract start's serial, else warning. Dates past the
    last prefilled row land in appends (D/F formulas shifted from the last row
    when they match the known patterns). daily_new is only proposed when the
    Daily cell is NOT a formula string (formulas are left alone); the daily
    chain uses the previous day's actual from the sheet OR this run's writes."""
    writes: list[dict] = []
    appends: list[dict] = []
    warnings: list[str] = []
    ci_date, ci_day = columns["date"], columns["day"]
    ci_actual = columns["actual"]
    # target/daily optional (header may lack them — degrade like the app does)
    ci_target, ci_daily = columns.get("target"), columns.get("daily")

    def cell(r: list, idx: int | None):
        if idx is None:
            return None
        return r[idx] if idx < len(r) else None

    data = [(i + 1, r) for i, r in enumerate(sheet_rows or [])
            if i + 1 > header_row and r]
    by_day: dict[int, int] = {}
    by_serial: dict[str, int] = {}
    actuals: dict[int, int] = {}
    max_day = 0
    last_rn = 0
    last_row: list | None = None
    start_serial = _traffic_iso_to_serial(contract_start_iso)
    anchor_checked = False
    for rn, r in data:
        b = _traffic_cell_int(cell(r, ci_day))
        if b is None:
            continue
        by_day.setdefault(b, rn)
        serial = _traffic_cell_int(cell(r, ci_date))
        if serial is not None:
            by_serial.setdefault(_traffic_serial_to_iso(serial), rn)
            if b == 1 and not anchor_checked:
                anchor_checked = True
                if serial != start_serial:
                    warnings.append(
                        f"Day-1 anchor mismatch: sheet serial {serial} "
                        f"({_traffic_serial_to_iso(serial)}) != contract start "
                        f"{contract_start_iso} (serial {start_serial})"
                    )
        d = _traffic_cell_int(cell(r, ci_actual))
        if d is not None:
            actuals[b] = d
        max_day = max(max_day, b)
        last_rn, last_row = rn, r

    for date in dates:
        day = _traffic_day(date, contract_start_iso)
        total = int(ga.get(date, 0))
        rn = by_day.get(day)
        if rn is None:
            clash_rn = by_serial.get(date)
            if clash_rn is not None:
                warnings.append(
                    f"{date}: sheet row {clash_rn} holds that date (serial) but its "
                    f"Day column disagrees (expected {day}) — skipped"
                )
                continue
            if day > max_day:
                # append past the last prefilled row; shift D/F formulas from it
                new_rn = last_rn + len(appends) + 1
                target = None
                daily = None
                if last_row is not None:
                    d_cell, f_cell = cell(last_row, ci_target), cell(last_row, ci_daily)
                    if isinstance(d_cell, str) and d_cell.strip().startswith("="):
                        m = _TRAFFIC_D_FORMULA_RE.match(d_cell.strip())
                        if m:
                            target = f"=D{int(m.group(1)) + (new_rn - last_rn)}+{m.group(2)}"
                        else:
                            warnings.append(f"{date}: last row's Target formula unrecognized — append Target left blank")
                    else:
                        t = _traffic_cell_int(d_cell)
                        if t is not None:
                            target = t
                        else:
                            warnings.append(f"{date}: last row's Target is not a plain number — append Target left blank")
                    if isinstance(f_cell, str) and f_cell.strip().startswith("="):
                        m = _TRAFFIC_F_FORMULA_RE.match(f_cell.strip())
                        if m:
                            shift = new_rn - last_rn
                            daily = f"=E{int(m.group(1)) + shift}-E{int(m.group(2)) + shift}"
                        else:
                            warnings.append(f"{date}: last row's Daily formula unrecognized — append Daily left blank")
                if daily is None:
                    prev = actuals.get(day - 1)
                    daily = total if (day == 1 or prev is None) else total - prev
                appends.append({"date": date, "day": day, "target": target,
                                "actual_new": total, "daily_new": daily})
                actuals[day] = total
                max_day = day
                continue
            warnings.append(f"{date}: no sheet row for Day {day} (sheet has Days up to {max_day}) — skipped")
            continue
        r = sheet_rows[rn - 1]
        f = cell(r, ci_daily)
        is_formula = isinstance(f, str) and f.strip().startswith("=")
        prev = actuals.get(day - 1)
        if day == 1:
            daily = total  # first contract day: Daily = Total
        elif prev is None:
            daily = None
            warnings.append(f"{date}: no previous-day actual in sheet — Daily left blank")
        else:
            daily = total - prev
        writes.append({
            "row": rn, "date": date, "day": day,
            "target_old": cell(r, ci_target),  # echoed verbatim (formula or number)
            "actual_old": _traffic_cell_int(cell(r, ci_actual)),
            "actual_new": total,
            "daily_old": f if is_formula else _traffic_cell_int(f),
            "daily_new": None if is_formula else daily,
            "daily_is_formula": is_formula,
        })
        actuals[day] = total
    return {"writes": writes, "appends": appends, "warnings": warnings}


async def _traffic_ga_totals(token: str, property_id: str, dates: list[str],
                             contract_start_iso: str) -> dict[str, int]:
    """One runReport per date (deterministic, no row-order ambiguity): cumulative
    totalUsers from contract_start → date. Semaphore(2) + gather; ≤92 dates/run."""
    sem = asyncio.Semaphore(2)
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

    async with httpx.AsyncClient(timeout=30) as c:
        async def one(d: str) -> tuple[str, int]:
            async with sem:
                r = await c.post(url, headers=hdr, json={
                    "dateRanges": [{"startDate": contract_start_iso, "endDate": d}],
                    "metrics": [{"name": "totalUsers"}],
                })
            if r.status_code >= 400:
                raise HTTPException(502, f"GA {d}: {r.status_code} {r.text[:200]}")
            rows = r.json().get("rows") or []
            return d, int(rows[0]["metricValues"][0]["value"]) if rows else 0

        pairs = await asyncio.gather(*[one(d) for d in dates])
    return dict(pairs)


async def _traffic_sheet_read(token: str, sheet_id: str, tab: str) -> list[list]:
    """Raw values of the tab (A1:F5000, FORMULA render so D/F formulas are visible)."""
    hdr = {"Authorization": f"Bearer {token}"}
    rng = urllib.parse.quote(f"{tab}!A1:F5000")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
           "?valueRenderOption=FORMULA")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=hdr)
        if r.status_code != 200:
            raise HTTPException(502, f"Sheet read failed: {r.status_code} {r.text[:200]}")
        return r.json().get("values", [])


class TrafficAnalyzeReq(BaseModel):
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None

    model_config = {"populate_by_name": True}


class TrafficWriteItem(BaseModel):
    row: int
    date: str
    day: int
    target_old: str | float | int | None = None  # echoed verbatim (formula or number)
    actual_old: int | None = None
    actual_new: int
    daily_old: str | float | int | None = None  # echoed verbatim (formula or number)
    daily_new: int | None = None
    daily_is_formula: bool = False


class TrafficAppendItem(BaseModel):
    date: str
    day: int
    target: str | float | int | None = None  # shifted formula, plain number, or None
    actual_new: int
    daily_new: str | float | int | None = None  # shifted formula or computed diff


class TrafficApplyReq(BaseModel):
    sheet_writes: list[TrafficWriteItem] = []
    appends: list[TrafficAppendItem] = []


@router.post("/api/thailandnow/traffic/analyze")
async def traffic_analyze(req: TrafficAnalyzeReq) -> dict:
    """TRAFFIC — diff preview, ZERO writes. Defaults to today (Asia/Bangkok).
    Flow: config → dates → SA token → GA cumulative totals → sheet read →
    proposed writes → paste-ready text lines."""
    cfg = _ga_config()
    today = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d")
    frm = (req.from_ or today)[:10]
    to = (req.to or frm)[:10]
    try:
        dates = _traffic_dates(frm, to)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = await _sa_google_token(_TRAFFIC_SCOPES)
    ga = await _traffic_ga_totals(token, cfg["property_id"], dates, cfg["contract_start"])
    sheet_rows = await _traffic_sheet_read(token, cfg["sheet_id"], cfg["tab"])
    header_row, columns = _traffic_columns(sheet_rows)
    prop = _traffic_proposed_writes(sheet_rows, dates, ga, cfg["contract_start"],
                                    header_row, columns)
    # text lines recompute the daily chain over the sheet + this run's totals.
    # Targets live behind D formulas (FORMULA render shows the formula) — one
    # extra UNFORMATTED_VALUE read gets their computed numbers (app mirrors
    # this in sheets.ts); on failure the text just omits Target/Δ.
    ci_day, ci_actual = columns["day"], columns["actual"]
    ci_target = columns.get("target")  # optional column — text omits Target/Δ without it
    rng = urllib.parse.quote(f"{cfg['tab']}!A1:F5000")
    vurl = (f"https://sheets.googleapis.com/v4/spreadsheets/{cfg['sheet_id']}/values/{rng}"
            "?valueRenderOption=UNFORMATTED_VALUE")
    async with httpx.AsyncClient(timeout=30) as c:
        vr = await c.get(vurl, headers={"Authorization": f"Bearer {token}"})
    value_rows = vr.json().get("values", []) if vr.status_code == 200 else []
    actuals: dict[int, int] = {}
    targets: dict[int, int] = {}
    for r in value_rows[header_row:]:
        b = _traffic_cell_int(r[ci_day]) if len(r) > ci_day else None
        if b is None:
            continue
        d = _traffic_cell_int(r[ci_actual]) if len(r) > ci_actual else None
        if d is not None:
            actuals[b] = d
        t = _traffic_cell_int(r[ci_target]) if ci_target is not None and len(r) > ci_target else None
        if t is not None:
            targets[b] = t
    text_rows: list[dict] = []
    for date in dates:
        day = _traffic_day(date, cfg["contract_start"])
        total = int(ga.get(date, 0))
        prev = actuals.get(day - 1)
        daily = total if (day == 1 or prev is None) else total - prev
        actuals[day] = total
        text_rows.append({"date": date, "day": day, "total": total,
                          "daily": daily, "target": targets.get(day)})
    return {"rows": prop["writes"], "appends": prop["appends"],
            "warnings": prop["warnings"], "text": _traffic_text_lines(text_rows),
            "generated_at": datetime.now(ZoneInfo("Asia/Bangkok")).isoformat(timespec="seconds"),
            "from": frm, "to": to}


@router.post("/api/thailandnow/traffic/apply")
async def traffic_apply(req: TrafficApplyReq) -> dict:
    """TRAFFIC — write what the client echoed back from analyze (shapes
    re-validated, unknown keys ignored, capped 92 writes + 50 appends). Each
    write updates the FULL row A–F echoing every non-written column verbatim
    (A "", B serial recomputed from the date, C day, D/F formulas) — ONLY E
    (Actual) changes. Appends ride one values.append call."""
    cfg = _ga_config()
    tab = cfg["tab"]
    token = await _sa_google_token("https://www.googleapis.com/auth/spreadsheets")
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{cfg['sheet_id']}/values"
    failed: list[dict] = []
    written = 0

    async with httpx.AsyncClient(timeout=30) as c:
        async def one(w: TrafficWriteItem) -> None:
            nonlocal written
            try:
                rng = urllib.parse.quote(f"{tab}!A{w.row}:F{w.row}")
                f_cell = w.daily_new if w.daily_new is not None else w.daily_old
                # B as ISO date (USER_ENTERED re-parses to the same serial but
                # keeps date rendering; a bare serial int displays as a plain
                # number — the Aug 20–21 bug).
                values = [["", w.date, w.day,
                           w.target_old if w.target_old is not None else "",
                           w.actual_new, f_cell if f_cell is not None else ""]]
                r = await c.put(f"{base}/{rng}?valueInputOption=USER_ENTERED",
                                headers=hdr, json={"values": values})
                if r.status_code >= 400:
                    failed.append({"row": w.row, "error": f"{r.status_code} {r.text[:160]}"})
                else:
                    written += 1
            except Exception as e:  # per-write failure, never the whole batch
                failed.append({"row": w.row, "error": str(e)[:200]})

        results = await asyncio.gather(*[one(w) for w in req.sheet_writes[:92]],
                                       return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                failed.append({"row": -1, "error": str(res)[:200]})

        appended = 0
        items = req.appends[:92]  # matches the 92-dates run cap (validator caveat)
        if items:
            rows = [["", a.date, a.day,  # ISO date keeps date rendering (see apply note)
                     a.target if a.target is not None else "",
                     a.actual_new, a.daily_new if a.daily_new is not None else ""]
                    for a in items]
            rng = urllib.parse.quote(f"{tab}!A1")
            r = await c.post(f"{base}/{rng}:append?valueInputOption=USER_ENTERED",
                             headers=hdr, json={"values": rows})
            if r.status_code >= 400:
                failed.append({"row": -1, "error": f"append failed: {r.status_code} {r.text[:160]}"})
            else:
                appended = len(rows)
    return {"ok": not failed, "written": written, "appended": appended, "failed": failed}


# --- WordPress OP (publish-from-card) + Gem SEO -----------------------------


def _get_seo_gem_system_prompt() -> str:
    candidates = [
        Path(__file__).resolve().parent / "gems" / "gemini-gem-thailandnow-seo.md",
        Path(__file__).resolve().parent.parent / "assets" / "gemini-gem-thailandnow-seo.md",
        Path.home() / "Cephalon" / "10-knowledge" / "ai-workflow" / "gemini-gem-thailandnow-seo.md",
    ]
    content = ""
    for path in candidates:
        if path.exists():
            content = path.read_text(encoding="utf-8")
            break
    if content:
        return _extract_gem_body(content)
    return ""


AGY_BIN = os.path.expanduser("~/.local/bin/agy")


async def _agy_complete(prompt: str, timeout: float = 120.0, add_dir: str | None = None, effort: str = "medium") -> str | None:
    """Run agy CLI (AI-Pro Gemini quota) as a subprocess. Never raises (returns None on failure)."""
    try:
        cmd = [
            AGY_BIN,
            "--model",
            "gemini-3.6-flash",
            "--effort",
            effort,
            "--output-format",
            "text",
        ]
        if add_dir:
            cmd.extend(["--add-dir", add_dir])
        cmd.extend(["-p", prompt])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode == 0:
            return stdout.decode(errors="replace")
    except Exception:
        pass
    return None


def _parse_gemini_seo(text: str) -> dict | None:
    if not text or not isinstance(text, str):
        return None

    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).strip()
        s = re.sub(r"\n?```$", "", s).strip()

    keyphrases: list[str] = []
    metas: list[str] = []
    hashtags: str = ""
    ai_a: str = ""
    ai_b: list[str] = []

    current_section: str | None = None

    for line in s.splitlines():
        line_s = line.strip()

        if re.search(r"#+\s*(?:\d+[\.\)]\s*)?Focus Keyphrases", line, re.I):
            current_section = "keyphrases"
            continue
        elif re.search(r"#+\s*(?:\d+[\.\)]\s*)?Meta Descriptions?", line, re.I):
            current_section = "metas"
            continue
        elif re.search(r"#+\s*(?:\d+[\.\)]\s*)?Related Hashtags?", line, re.I):
            current_section = "hashtags"
            continue
        elif re.search(r"#+\s*(?:\d+[\.\)]\s*)?Version A", line, re.I) or (re.search(r"AI Summary", line, re.I) and "Version A" in line):
            current_section = "ai_a"
            continue
        elif re.search(r"#+\s*(?:\d+[\.\)]\s*)?Version B", line, re.I) or (re.search(r"Key Takeaways", line, re.I) and ("Version B" in line or "Key Takeaways" in line)):
            current_section = "ai_b"
            continue
        elif re.search(r"\*\*(?:AI Summary|Key Takeaways):?\*\*", line, re.I):
            if "AI Summary" in line:
                current_section = "ai_a"
                rem = re.sub(r"\*\*AI Summary:?\*\*", "", line).strip()
                if rem:
                    ai_a = (ai_a + " " + rem).strip() if ai_a else rem
                continue
            elif "Key Takeaways" in line:
                current_section = "ai_b"
                continue
        elif re.search(r"#+\s*(?:\d+[\.\)]\s*)?AI SEO Block", line, re.I):
            current_section = "ai_block"
            continue
        elif line_s.startswith("##") and not re.search(r"Version", line, re.I):
            current_section = None
            continue

        if not line_s:
            continue

        if current_section == "keyphrases":
            m = re.match(r"^\d+[\.\)]\s*(.+)", line_s)
            if m:
                item = m.group(1).strip()
                if " - Priority:" in item:
                    kp = item.split(" - Priority:")[0].strip()
                elif " Priority:" in item:
                    kp = item.split(" Priority:")[0].strip(" -:")
                else:
                    kp = item
                if kp:
                    keyphrases.append(kp)

        elif current_section == "metas":
            m = re.match(r"^\d+[\.\)]\s*(.+)", line_s)
            if m:
                item = m.group(1).strip()
                item_clean = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", item).strip()
                if item_clean:
                    metas.append(item_clean)

        elif current_section == "hashtags":
            if "#" in line_s:
                tags = re.findall(r"#\w+", line_s)
                if tags:
                    if hashtags:
                        hashtags += " " + " ".join(tags)
                    else:
                        hashtags = " ".join(tags)

        elif current_section == "ai_a":
            if not line_s.startswith("#") and not line_s.startswith("**Key Takeaways"):
                clean_l = re.sub(r"^\*\*AI Summary:?\*\*\s*", "", line_s).strip()
                if clean_l and not clean_l.lower().startswith("ai summary:"):
                    if ai_a:
                        ai_a += " " + clean_l
                    else:
                        ai_a = clean_l

        elif current_section == "ai_b":
            if line_s.startswith(("*", "-")):
                bullet = re.sub(r"^[\*\-]\s*", "", line_s).strip()
                if bullet and not bullet.lower().startswith("ai summary"):
                    ai_b.append(bullet)

    if not hashtags:
        all_tags = re.findall(r"#\w+", s)
        if all_tags:
            hashtags = " ".join(all_tags[:5])

    if not keyphrases or not metas or not hashtags or not ai_a or not ai_b:
        return None

    def _clean(x: str) -> str:
        return re.sub(r"[\*`]+", "", x).strip()  # strip markdown emphasis/code → clean plaintext SEO fields

    return {
        "keyphrases": [_clean(k) for k in keyphrases],
        "metas": [_clean(m) for m in metas],
        "hashtags": _clean(hashtags),
        "ai_a": _clean(ai_a),
        "ai_b": [_clean(b) for b in ai_b[:5]],
    }


async def _generate_event_seo(title: str, body: str, category: str = "Events") -> tuple[dict, str]:
    gem_core = _get_seo_gem_system_prompt()
    article = f"TITLE: {title}\nBODY: {body}\nCategory: {category}"
    agy_prompt = (
        f"{gem_core}\n\n"
        f"--- ARTICLE ---\n"
        f"{article}\n\n"
        f"Now output ONLY the four SEO sections (Focus Keyphrases, Meta Descriptions, "
        f"Related Hashtags, AI SEO Block with Version A and Version B), no preamble."
    )

    # 1. PRIMARY: agy (AI-Pro Gemini quota)
    raw = await _agy_complete(agy_prompt)
    if raw:
        parsed = _parse_gemini_seo(raw)
        if parsed:
            return parsed, "gemini-3.6-flash (agy)"

    # 2. FALLBACK: glm-5 via zai_message (OmniRoute)
    try:
        raw = await zai_message(article, max_tokens=4096, system=gem_core, model="glm-5", timeout=120.0)
        if raw:
            parsed = _parse_gemini_seo(raw)
            if parsed:
                return parsed, "glm-5"
    except Exception:
        pass

    raise HTTPException(503, "SEO generation failed: agy + glm-5 both failed")


async def _wp_upload_media_bytes(
    image_bytes: bytes, filename: str, content_type: str = "image/jpeg", alt_text: str = ""
) -> dict:
    """Upload media bytes to WP REST API POST /wp-json/wp/v2/media (Basic auth) and set alt text."""
    url, user, pwd = _wp_creds()
    headers = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    async with httpx.AsyncClient(timeout=30, auth=(user, pwd), follow_redirects=True) as c:
        r = await c.post(f"{url}/wp-json/wp/v2/media", headers=headers, content=image_bytes)
        if r.status_code >= 400:
            raise HTTPException(502, f"WP media upload failed: {r.status_code} {r.text[:200]}")
        media = r.json()
        media_id = media.get("id")
        source_url = media.get("source_url") or (media.get("guid") or {}).get("rendered", "")

        if media_id and alt_text:
            try:
                r_alt = await c.post(
                    f"{url}/wp-json/wp/v2/media/{media_id}",
                    json={"alt_text": alt_text},
                )
                if r_alt.status_code >= 400:
                    await c.post(
                        f"{url}/wp-json/wp/v2/media/{media_id}",
                        json={"meta": {"_wp_attachment_image_alt": alt_text}},
                    )
            except Exception:
                pass

        return {"id": media_id, "source_url": source_url, "alt": alt_text}


async def _agy_describe_image(image_bytes: bytes, ext: str = "jpg") -> str:
    """Describe image in one concise sentence for WordPress SEO alt text via agy vision."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = Path(tmpdir) / f"image.{ext}"
        img_path.write_bytes(image_bytes)
        alt = await _agy_complete(
            "describe this image in one concise sentence for WordPress SEO alt text, output only the description",
            add_dir=tmpdir,
            effort="low",
        )
        if alt:
            return alt.strip().strip('"\'').strip()
    return ""


def _clean_gutenberg_attributes(html_text: str) -> str:
    s = re.sub(r"</?(?:span|font)\b[^>]*>", "", html_text, flags=re.IGNORECASE)

    def _clean_tag(m: re.Match) -> str:
        tag = m.group(1)
        tag_lower = tag.lower()
        if tag_lower in ("figure", "figcaption"):
            return m.group(0)
        attrs = m.group(2)
        class_m = re.search(r'class=["\'](wp-block-[^"\']+)["\']', attrs, re.IGNORECASE)
        href_m = re.search(r'href=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        src_m = re.search(r'src=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        alt_m = re.search(r'alt=["\']([^"\']*)["\']', attrs, re.IGNORECASE)

        new_attrs = []
        if class_m:
            new_attrs.append(f'class="{class_m.group(1)}"')
        if href_m:
            new_attrs.append(f'href="{href_m.group(1)}"')
            new_attrs.append('target="_blank"')
            new_attrs.append('rel="noopener"')
        if src_m:
            new_attrs.append(f'src="{src_m.group(1)}"')
        if alt_m:
            new_attrs.append(f'alt="{alt_m.group(1)}"')

        attr_str = (" " + " ".join(new_attrs)) if new_attrs else ""
        return f"<{tag}{attr_str}>"

    return re.sub(r"<([a-zA-Z0-9]+)\b([^>]*)>", _clean_tag, s)


MONTHS_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12
}


def _parse_event_dates(date_str: str, default_year: int | None = None) -> tuple[str, str]:
    """Parse human event date strings (e.g. '7 - 9 August, 2026', 'August 7 to 9, 2026') into (YYYY-MM-DD, YYYY-MM-DD)."""
    if not date_str:
        return "", ""
    s = date_str.strip()
    year = default_year or datetime.now().year
    m_year = re.search(r"\b(20\d{2})\b", s)
    if m_year:
        year = int(m_year.group(1))

    s_clean = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", s, flags=re.I)

    # Pattern 1: '7 - 9 August, 2026' or '7 to 9 August 2026'
    m1 = re.search(r"(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})\s+([a-zA-Z]+)", s_clean, re.I)
    if m1:
        d1, d2, m_name = int(m1.group(1)), int(m1.group(2)), m1.group(3).lower()
        if m_name in MONTHS_MAP:
            mo = MONTHS_MAP[m_name]
            return f"{year:04d}-{mo:02d}-{d1:02d}", f"{year:04d}-{mo:02d}-{d2:02d}"

    # Pattern 2: 'August 7 - 9, 2026' or 'August 7 to 9, 2026'
    m2 = re.search(r"([a-zA-Z]+)\s+(\d{1,2})\s*(?:-|–|—|to|through)\s*(\d{1,2})", s_clean, re.I)
    if m2:
        m_name, d1, d2 = m2.group(1).lower(), int(m2.group(2)), int(m2.group(3))
        if m_name in MONTHS_MAP:
            mo = MONTHS_MAP[m_name]
            return f"{year:04d}-{mo:02d}-{d1:02d}", f"{year:04d}-{mo:02d}-{d2:02d}"

    # Pattern 3: 'July 30 - August 2, 2026'
    m3 = re.search(r"([a-zA-Z]+)\s+(\d{1,2})\s*(?:-|–|—|to|through)\s*([a-zA-Z]+)\s+(\d{1,2})", s_clean, re.I)
    if m3:
        m1_name, d1, m2_name, d2 = m3.group(1).lower(), int(m3.group(2)), m3.group(3).lower(), int(m3.group(4))
        if m1_name in MONTHS_MAP and m2_name in MONTHS_MAP:
            mo1, mo2 = MONTHS_MAP[m1_name], MONTHS_MAP[m2_name]
            return f"{year:04d}-{mo1:02d}-{d1:02d}", f"{year:04d}-{mo2:02d}-{d2:02d}"

    # Pattern 4: Single date 'August 7, 2026' or '7 August 2026'
    m4 = re.search(r"(\d{1,2})\s+([a-zA-Z]+)", s_clean, re.I)
    if m4:
        d1, m_name = int(m4.group(1)), m4.group(2).lower()
        if m_name in MONTHS_MAP:
            mo = MONTHS_MAP[m_name]
            dt = f"{year:04d}-{mo:02d}-{d1:02d}"
            return dt, dt

    m5 = re.search(r"([a-zA-Z]+)\s+(\d{1,2})", s_clean, re.I)
    if m5:
        m_name, d1 = m5.group(1).lower(), int(m5.group(2))
        if m_name in MONTHS_MAP:
            mo = MONTHS_MAP[m_name]
            dt = f"{year:04d}-{mo:02d}-{d1:02d}"
            return dt, dt

    return "", ""


def _slugify_anchor(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", s)


def _render_doc_paragraph_html(para: dict) -> str:
    parts = []
    for pe in para.get("elements", []):
        tr = pe.get("textRun")
        if not tr:
            continue
        content = tr.get("content", "")
        if not content:
            continue
        t_style = tr.get("textStyle", {})
        has_trailing_nl = content.endswith("\n")
        inner = content[:-1] if has_trailing_nl else content
        if inner:
            inner_esc = html.escape(inner)
            if t_style.get("bold"):
                inner_esc = f"<strong>{inner_esc}</strong>"
            if t_style.get("italic"):
                inner_esc = f"<em>{inner_esc}</em>"
            link = t_style.get("link", {}).get("url")
            if link:
                inner_esc = f'<a href="{link}">{inner_esc}</a>'
            parts.append(inner_esc)
    return "".join(parts).strip()


def _extract_google_doc_data(
    doc_json: dict,
    fallback_text: str = "",
    doc_html: str = "",
    card_name: str = "",
    default_year: int | None = None,
    append_year: bool = False,
    is_article: bool = False,
    is_blog: bool = False,
) -> dict:
    year = default_year or datetime.now().year
    card_title = re.sub(r"^(Article|Event|Blog)\s*\|\s*", "", card_name, flags=re.IGNORECASE).strip() or card_name

    if not doc_json or not isinstance(doc_json, dict) or "body" not in doc_json:
        doc_title = _extract_doc_title(doc_html, fallback_text)
        title = doc_title or card_title
        if append_year and not is_article and not re.search(r"\b20\d{2}\s*$", title):
            title = f"{title} {year}"
        return {
            "title": title,
            "location": "",
            "dates_raw": "",
            "start_date": "",
            "end_date": "",
            "clean_body_text": fallback_text,
            "body_content": [],
            "inline_objects": {},
            "content_start_idx": 0,
        }

    body_content = doc_json.get("body", {}).get("content", [])
    inline_objects = doc_json.get("inlineObjects", {})

    # 1. Find H1 / Title
    h1_idx = -1
    doc_title = ""
    for i, el in enumerate(body_content):
        para = el.get("paragraph")
        if not para:
            continue
        style = (para.get("paragraphStyle") or {}).get("namedStyleType", "")
        if style in ("HEADING_1", "TITLE"):
            h1_idx = i
            parts = [pe.get("textRun", {}).get("content", "") for pe in para.get("elements", [])]
            doc_title = "".join(parts).strip()
            doc_title = re.sub(r"\s+", " ", doc_title)
            break

    # Blog: title = first non-empty paragraph (any style); body starts right after it.
    blog_title = ""
    blog_start_idx = -1
    if is_blog:
        for i, el in enumerate(body_content):
            para = el.get("paragraph")
            if not para:
                continue
            t = "".join(pe.get("textRun", {}).get("content", "") for pe in para.get("elements", [])).strip()
            if t:
                blog_title = re.sub(r"\s+", " ", t)
                blog_start_idx = i + 1
                break

    title = (blog_title if is_blog else "") or doc_title or card_title
    if append_year and not is_article and not is_blog and not re.search(r"\b20\d{2}\s*$", title):
        title = f"{title} {year}"

    location = ""
    dates_raw = ""
    start_date = ""
    end_date = ""

    if is_article or is_blog:
        # Articles and Blogs have no location/dates headers below the title
        if is_blog and blog_start_idx >= 0:
            content_start_idx = blog_start_idx
        else:
            content_start_idx = (h1_idx + 1) if h1_idx >= 0 else 0
    else:
        # Events have Location and Dates in the paragraphs following H1
        idx = (h1_idx + 1) if h1_idx >= 0 else 0
        if h1_idx >= 0:
            while idx < len(body_content):
                para = body_content[idx].get("paragraph")
                if para:
                    t = "".join(pe.get("textRun", {}).get("content", "") for pe in para.get("elements", [])).strip()
                    if t:
                        location = t
                        idx += 1
                        break
                idx += 1

            while idx < len(body_content):
                para = body_content[idx].get("paragraph")
                if para:
                    t = "".join(pe.get("textRun", {}).get("content", "") for pe in para.get("elements", [])).strip()
                    if t:
                        dates_raw = t
                        idx += 1
                        break
                idx += 1

        content_start_idx = idx
        start_date, end_date = _parse_event_dates(dates_raw, default_year=year)

    body_paras = []
    for el in body_content[content_start_idx:]:
        para = el.get("paragraph")
        if para:
            t = "".join(pe.get("textRun", {}).get("content", "") for pe in para.get("elements", [])).strip()
            if t and not re.match(r"^\[[a-z0-9]+\]", t):
                body_paras.append(t)
    clean_body_text = "\n\n".join(body_paras)

    return {
        "title": title,
        "location": location,
        "dates_raw": dates_raw,
        "start_date": start_date,
        "end_date": end_date,
        "clean_body_text": clean_body_text,
        "body_content": body_content,
        "inline_objects": inline_objects,
        "content_start_idx": content_start_idx,
    }


def _build_gutenberg_from_doc_ast(
    body_content: list[dict],
    img_map: dict[str, dict],
    takeaways: list[str],
    content_start_idx: int = 0,
    is_article: bool = False,
    is_blog: bool = False,
    include_takeaways: bool = True,
) -> str:
    takeaway_items = "".join(
        f"<!-- wp:list-item -->\n<li>{html.escape(t)}</li>\n<!-- /wp:list-item -->\n\n" for t in takeaways
    ).strip()

    group_block = (
        f'<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->\n\n'
        f'<!-- wp:group {{"style":{{"color":{{"background":"#efefef"}}}},"layout":{{"type":"constrained"}}}} -->\n'
        f'<div class="wp-block-group has-background" style="background-color:#efefef"><!-- wp:heading {{"anchor":"h-key-takeaways"}} -->\n'
        f'<h2 id="h-key-takeaways" class="wp-block-heading">Key Takeaways</h2>\n'
        f'<!-- /wp:heading -->\n\n'
        f'<!-- wp:list -->\n'
        f'<ul class="wp-block-list">\n{takeaway_items}\n</ul>\n'
        f'<!-- /wp:list --></div>\n'
        f'<!-- /wp:group -->\n\n'
        f'<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->'
    )

    three_enters = (
        '<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->\n\n'
        '<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->\n\n'
        '<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->'
    )

    blocks: list[str] = []
    group_inserted = False
    idx = content_start_idx

    pending_list: list[str] = []

    def _flush_list() -> None:
        """Emit accumulated bullet paragraphs as one wp:list block (Ben-style grouping)."""
        nonlocal group_inserted
        if not pending_list:
            return
        if include_takeaways and not group_inserted:
            blocks.append(group_block)
            group_inserted = True
        items = "".join(
            f"<!-- wp:list-item -->\n<li>{it}</li>\n<!-- /wp:list-item -->\n\n"
            for it in pending_list
        ).strip()
        blocks.append(
            f'<!-- wp:list -->\n'
            f'<ul class="wp-block-list">\n{items}\n</ul>\n'
            f'<!-- /wp:list -->'
        )
        pending_list.clear()

    while idx < len(body_content):
        el = body_content[idx]
        idx += 1
        para = el.get("paragraph")
        if not para:
            continue

        style = (para.get("paragraphStyle") or {}).get("namedStyleType", "")
        elements = para.get("elements", [])
        is_bullet = bool(para.get("bullet"))

        # Check for image if not an article
        img_id = None
        if not is_article:
            for pe in elements:
                io = pe.get("inlineObjectElement")
                if io:
                    img_id = io.get("inlineObjectId")
                    break

        if img_id:
            _flush_list()
            if include_takeaways and not group_inserted:
                blocks.append(group_block)
                group_inserted = True
            media = img_map.get(img_id)
            if media:
                wp_media_id = media.get("id", 0)
                src = media.get("source_url", "")
                alt = media.get("alt", "")
                caption = media.get("caption", "")
                cap_html = f'<figcaption class="wp-element-caption">{html.escape(caption)}</figcaption>' if caption else ""
                img_block = (
                    f'<!-- wp:image {{"id":{wp_media_id},"sizeSlug":"large","linkDestination":"none"}} -->\n'
                    f'<figure class="wp-block-image size-large"><img src="{src}" alt="{html.escape(alt)}" class="wp-image-{wp_media_id}"/>{cap_html}</figure>\n'
                    f'<!-- /wp:image -->'
                )
                blocks.append(img_block)
                blocks.append('<!-- wp:paragraph -->\n<p></p>\n<!-- /wp:paragraph -->')
            continue

        p_text = _render_doc_paragraph_html(para)
        if not p_text:
            continue
        if re.match(r"^\[[a-z0-9]+\]", p_text):
            continue

        if is_bullet:
            pending_list.append(p_text)
            continue

        _flush_list()

        if style == "HEADING_2":
            if include_takeaways and not group_inserted:
                blocks.append(group_block)
                group_inserted = True
            h_text = "".join(pe.get("textRun", {}).get("content", "") for pe in elements).strip()
            anchor = f"h-{_slugify_anchor(h_text)}"
            if is_article or is_blog:
                h_block = (
                    f'<!-- wp:heading {{"anchor":"{anchor}"}} -->\n'
                    f'<h2 id="{anchor}" class="wp-block-heading"><strong>{html.escape(h_text)}</strong></h2>\n'
                    f'<!-- /wp:heading -->\n\n'
                    f'{three_enters}'
                )
            else:
                h_block = (
                    f'<!-- wp:heading {{"anchor":"{anchor}"}} -->\n'
                    f'<h2 id="{anchor}" class="wp-block-heading"><strong>{html.escape(h_text)}</strong></h2>\n'
                    f'<!-- /wp:heading -->'
                )
            blocks.append(h_block)
        else:
            para_block = f'<!-- wp:paragraph -->\n<p>{p_text}</p>\n<!-- /wp:paragraph -->'
            blocks.append(para_block)

    _flush_list()

    if include_takeaways and not group_inserted:
        if len(blocks) >= 2:
            blocks.insert(2, group_block)
        elif blocks:
            blocks.insert(1, group_block)
        else:
            blocks.append(group_block)

    return "\n\n".join(blocks)


def _convert_google_html_to_gutenberg(doc_html: str, img_map: dict[str, dict] | None = None) -> str:
    if not doc_html:
        return ""

    body_match = re.search(r"<body[^>]*>(.*?)</body>", doc_html, re.IGNORECASE | re.DOTALL)
    content = body_match.group(1) if body_match else doc_html

    # Replace headings h1, h2, h3 -> <h2 class="wp-block-heading">
    content = re.sub(r"<h[123]\b[^>]*>", '<h2 class="wp-block-heading">', content, flags=re.IGNORECASE)
    content = re.sub(r"</h[123]>", "</h2>", content, flags=re.IGNORECASE)

    # Replace paragraphs <p ...> -> <p class="wp-block-paragraph">
    content = re.sub(r"<p\b[^>]*>", '<p class="wp-block-paragraph">', content, flags=re.IGNORECASE)

    # Replace list items/lists
    content = re.sub(r"<ul\b[^>]*>", '<ul class="wp-block-list">', content, flags=re.IGNORECASE)
    content = re.sub(r"<ol\b[^>]*>", '<ol class="wp-block-list">', content, flags=re.IGNORECASE)

    # Replace <img> tags
    img_map = img_map or {}

    def _replace_img(m: re.Match) -> str:
        img_tag = m.group(0)
        src_m = re.search(r'src=["\']([^"\']+)["\']', img_tag, re.IGNORECASE)
        if src_m:
            src = src_m.group(1)
            if src in img_map:
                info = img_map[src]
                img_id = info.get("id", 0)
                source_url = info.get("source_url", src)
                alt = info.get("alt", "")
                return f'<figure class="wp-block-image size-large"><img src="{source_url}" alt="{alt}" class="wp-image-{img_id}"/></figure>'
        return ""

    content = re.sub(r"<img\b[^>]*>", _replace_img, content, flags=re.IGNORECASE)
    content = _clean_gutenberg_attributes(content)
    content = re.sub(r'<p class="wp-block-paragraph">\s*(?:&nbsp;)?\s*</p>', '', content, flags=re.IGNORECASE)
    return content.strip()


def _seo_best(content: str, keyphrases: list[str], metas: list[str] | None = None, title: str = "") -> tuple[str, str]:
    if not keyphrases:
        return "", (metas[0] if metas and len(metas) > 0 else "")

    h2_headings = " ".join(re.findall(r"<h2[^>]*>(.*?)</h2>", content, re.IGNORECASE | re.DOTALL))
    h2_clean = re.sub(r"<[^>]+>", "", h2_headings)

    first_para_m = re.search(r"<p[^>]*>(.*?)</p>", content, re.IGNORECASE | re.DOTALL)
    first_para_clean = re.sub(r"<[^>]+>", "", first_para_m.group(1)) if first_para_m else ""

    body_clean = re.sub(r"<[^>]+>", "", content)

    title_low = title.lower()
    h2_low = h2_clean.lower()
    first_para_low = first_para_clean.lower()
    body_low = body_clean.lower()

    best_kp = keyphrases[0]
    best_score = -1

    for kp in keyphrases:
        kp_low = kp.lower().strip()
        if not kp_low:
            continue
        score = (
            title_low.count(kp_low) * 3
            + h2_low.count(kp_low) * 2
            + first_para_low.count(kp_low) * 2
            + body_low.count(kp_low) * 1
        )
        if score > best_score:
            best_score = score
            best_kp = kp

    best_meta = metas[0] if (metas and len(metas) > 0) else ""
    if metas:
        kp_words = set(re.findall(r"\w+", best_kp.lower()))
        best_meta_score = -1
        for m in metas:
            m_low = m.lower()
            m_words = set(re.findall(r"\w+", m_low))
            overlap = len(kp_words & m_words)
            exact_bonus = 5 if best_kp.lower() in m_low else 0
            score = overlap + exact_bonus
            if score > best_meta_score:
                best_meta_score = score
                best_meta = m

    return best_kp, best_meta


def _convert_text_to_gutenberg(doc_text: str) -> str:
    """Convert plain text to Gutenberg paragraph blocks when HTML export is empty/minimal.
    Strips leading standalone social-platform labels (e.g. 'Facebook', 'Instagram')."""
    if not doc_text or not doc_text.strip():
        return ""
    lines = doc_text.splitlines()
    start_idx = 0
    for idx, line in enumerate(lines):
        s = line.strip().strip("\ufeff").strip()
        if s:
            if re.match(r"^(facebook|instagram|twitter|x|tiktok|threads|youtube|linkedin)\s*:?$", s, re.I):
                start_idx = idx + 1
            break
    cleaned_text = "\n".join(lines[start_idx:]).strip()
    if not cleaned_text:
        return ""

    blocks = re.split(r"\n\s*\n", cleaned_text)
    p_blocks = []
    for b in blocks:
        s = b.strip()
        if not s:
            continue
        s_html = s.replace("\n", "<br />")
        p_blocks.append(f'<p class="wp-block-paragraph">{s_html}</p>')
    return "\n\n".join(p_blocks)


def _extract_doc_title(doc_html: str, doc_text: str = "") -> str:
    """The writer's headline from the Google Doc — first <h1>/<h2>, else '' if no heading found
    (caller falls back to card name)."""
    if doc_html:
        m = re.search(r"<h[12][^>]*>(.*?)</h[12]>", doc_html, re.S | re.I)
        if m:
            t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if t:
                return t[:200]
    return ""


@router.get("/api/thailandnow/events/to-publish")
async def events_to_publish() -> dict:
    """List Trello cards from the 'To publish (NAZ + TOON)' list (685686f5a5d5ec7d657af3c6)."""
    cards_raw = await _trello("GET", "/lists/685686f5a5d5ec7d657af3c6/cards", {"fields": "id,name"})
    cards = cards_raw if isinstance(cards_raw, list) else []
    return {
        "cards": [
            {"id": c["id"], "name": c["name"]}
            for c in cards
            if isinstance(c, dict) and "id" in c and "name" in c
        ]
    }


@router.post("/api/thailandnow/events/analyze-card")
@router.post("/api/thailandnow/articles/analyze-card")
async def analyze_card(payload: dict = Body(default={})) -> dict:
    """Fetch card attachments & desc, find Google Doc, extract text, and run Gem SEO.
    Supports both Events (default) and Articles (kind='article' or card name starting with 'Article |')."""
    card_id = (payload.get("card_id") or "").strip()
    if not card_id:
        raise HTTPException(400, "card_id required")

    kind = (payload.get("kind") or "").strip().lower()

    card = await _trello("GET", f"/cards/{card_id}", {"fields": "name,desc,due"})
    if not isinstance(card, dict) or not card.get("name"):
        raise HTTPException(404, f"Trello card {card_id} not found")
    card_name = card.get("name", "")
    card_desc = card.get("desc", "")

    if not kind:
        cn = card_name.lower()
        if cn.startswith("blog"):
            kind = "blog"
        elif cn.startswith("article"):
            kind = "article"
        else:
            kind = "event"
    is_article = (kind == "article")
    is_blog = (kind == "blog")

    due_raw = card.get("due")
    year = datetime.now().year
    if due_raw:
        try:
            year = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00")).year
        except Exception:
            m = re.match(r"^(\d{4})", str(due_raw).strip())
            if m:
                year = int(m.group(1))

    attachments = await _trello("GET", f"/cards/{card_id}/attachments", {"fields": "name,url"})
    doc_id = None
    if isinstance(attachments, list):
        for att in attachments:
            if isinstance(att, dict) and att.get("url") and "docs.google.com/document" in att["url"]:
                m = re.search(r"/document/d/([^/]+)", att["url"])
                if m:
                    doc_id = m.group(1)
                    break

    if not doc_id and card_desc and "docs.google.com/document" in card_desc:
        m = re.search(r"/document/d/([^/]+)", card_desc)
        if m:
            doc_id = m.group(1)

    if not doc_id:
        raise HTTPException(404, f"No Google Doc attachment or link found on card {card_id}")

    token = await _google_token()
    doc_json = await _drive_read_doc_json(token, doc_id)
    doc_text = await _drive_read_doc(token, doc_id) if not doc_json else ""
    doc_html = await _drive_read_doc_html(token, doc_id) if not doc_json else ""

    parsed = _extract_google_doc_data(
        doc_json,
        fallback_text=doc_text,
        doc_html=doc_html,
        card_name=card_name,
        default_year=year,
        append_year=False,
        is_article=is_article,
        is_blog=is_blog,
    )
    title = parsed["title"]
    clean_body = parsed["clean_body_text"] or doc_text
    category = "Blogs" if is_blog else ("Articles" if is_article else "Events")

    seo, seo_model = await _generate_event_seo(title, clean_body, category)
    best_kp, best_meta = _seo_best(clean_body, seo["keyphrases"], seo["metas"], title=title)
    seo["focus_keyphrase"] = best_kp
    seo["meta_description"] = best_meta

    return {
        "card_id": card_id,
        "title": title,
        "kind": kind,
        "location": parsed["location"],
        "dates_raw": parsed["dates_raw"],
        "start_date": parsed["start_date"],
        "end_date": parsed["end_date"],
        "doc_text": clean_body,
        "seo": seo,
        "seo_model": seo_model,
    }


@router.post("/api/thailandnow/events/publish-from-card")
@router.post("/api/thailandnow/articles/publish-from-card")
async def publish_event_from_card(payload: dict = Body(default={})) -> dict:
    card_id = (payload.get("card_id") or "").strip()
    if not card_id:
        raise HTTPException(400, "card_id required")

    kind = (payload.get("kind") or "").strip().lower()

    card = await _trello("GET", f"/cards/{card_id}", {"fields": "name,desc,due"})
    if not isinstance(card, dict) or not card.get("name"):
        raise HTTPException(404, f"Trello card {card_id} not found")
    card_name = card.get("name", "")
    card_desc = card.get("desc", "")

    if not kind:
        cn = card_name.lower()
        if cn.startswith("blog"):
            kind = "blog"
        elif cn.startswith("article"):
            kind = "article"
        else:
            kind = "event"
    is_article = (kind == "article")
    is_blog = (kind == "blog")

    due_raw = card.get("due")
    year = datetime.now().year
    if due_raw:
        try:
            year = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00")).year
        except Exception:
            m = re.match(r"^(\d{4})", str(due_raw).strip())
            if m:
                year = int(m.group(1))

    attachments = await _trello("GET", f"/cards/{card_id}/attachments", {"fields": "name,url"})
    doc_id = None
    if isinstance(attachments, list):
        for att in attachments:
            if isinstance(att, dict) and att.get("url") and "docs.google.com/document" in att["url"]:
                m = re.search(r"/document/d/([^/]+)", att["url"])
                if m:
                    doc_id = m.group(1)
                    break

    if not doc_id and card_desc and "docs.google.com/document" in card_desc:
        m = re.search(r"/document/d/([^/]+)", card_desc)
        if m:
            doc_id = m.group(1)

    if not doc_id:
        raise HTTPException(404, f"No Google Doc attachment or link found on card {card_id}")

    token = await _google_token()
    doc_json = await _drive_read_doc_json(token, doc_id)
    doc_text = await _drive_read_doc(token, doc_id) if not doc_json else ""
    doc_html = await _drive_read_doc_html(token, doc_id) if not doc_json else ""

    parsed = _extract_google_doc_data(
        doc_json,
        fallback_text=doc_text,
        doc_html=doc_html,
        card_name=card_name,
        default_year=year,
        append_year=(kind == "event"),
        is_article=is_article,
        is_blog=is_blog,
    )
    title = parsed["title"]
    clean_body = parsed["clean_body_text"] or doc_text
    category = "Blogs" if is_blog else ("Articles" if is_article else "Events")

    img_map: dict[str, dict] = {}
    images_uploaded = 0

    # Inline image uploads only for Events (Articles leave 3 enters under each H2 for manual image placement)
    if not is_article:
        inline_objs = parsed.get("inline_objects") or {}
        if inline_objs:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                for idx, (k, v) in enumerate(inline_objs.items()):
                    emb = (v.get("inlineObjectProperties") or {}).get("embeddedObject") or {}
                    uri = (emb.get("imageProperties") or {}).get("contentUri")
                    if not uri:
                        continue
                    try:
                        r_img = await c.get(uri)
                        if r_img.status_code == 200:
                            c_type = r_img.headers.get("content-type", "image/png")
                            ext = "png" if "png" in c_type else ("webp" if "webp" in c_type else "jpg")
                            alt = await _agy_describe_image(r_img.content, ext=ext)
                            media = await _wp_upload_media_bytes(
                                r_img.content,
                                filename=f"event-{card_id}-{idx+1}.{ext}",
                                content_type=f"image/{ext}",
                                alt_text=alt,
                            )
                            if media and media.get("id"):
                                img_map[k] = media
                                images_uploaded += 1
                    except Exception:
                        pass
        elif doc_html:
            img_urls = re.findall(r'<img\b[^>]*?\bsrc=["\']([^"\']+)["\']', doc_html, re.IGNORECASE)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                for idx, src in enumerate(img_urls):
                    if src in img_map:
                        continue
                    try:
                        img_bytes: bytes | None = None
                        ext = "jpg"
                        if src.startswith("data:"):
                            mext = re.match(r"data:image/([a-zA-Z0-9.+-]+)", src)
                            ext = (mext.group(1).lower().split("+")[0] if mext else "png")
                            ext = "jpg" if ext in ("jpeg", "jpe") else (ext.split(".")[0] or "png")
                            b64 = src.split(",", 1)[1] if "," in src else ""
                            img_bytes = base64.b64decode(b64) if b64 else None
                        elif src.startswith("http"):
                            r_img = await c.get(src)
                            if r_img.status_code != 200:
                                continue
                            c_type = r_img.headers.get("content-type", "image/jpeg")
                            ext = "png" if "png" in c_type else ("webp" if "webp" in c_type else "jpg")
                            img_bytes = r_img.content
                        else:
                            continue
                        if not img_bytes:
                            continue
                        alt = await _agy_describe_image(img_bytes, ext=ext)
                        media = await _wp_upload_media_bytes(
                            img_bytes,
                            filename=f"event-{card_id}-{idx+1}.{ext}",
                            content_type=f"image/{ext}",
                            alt_text=alt,
                        )
                        if media and media.get("id"):
                            img_map[src] = media
                            images_uploaded += 1
                    except Exception:
                        pass

    seo_data, seo_model = await _generate_event_seo(title, clean_body, category)
    takeaways = [] if is_blog else (seo_data.get("ai_b") or [])

    if doc_json and parsed.get("body_content"):
        content = _build_gutenberg_from_doc_ast(
            parsed["body_content"],
            img_map,
            takeaways,
            parsed["content_start_idx"],
            is_article=is_article,
            is_blog=is_blog,
            include_takeaways=(not is_blog),
        )
    else:
        # Fallback when no AST available
        doc_title = _extract_doc_title(doc_html, doc_text)
        _body_html = doc_html
        if doc_title:
            _tm = re.search(r"<h[12][^>]*>(.*?)</h[12]>", doc_html, re.S | re.I)
            if _tm and re.sub(r"<[^>]+>", "", _tm.group(1)).strip() == doc_title:
                _body_html = doc_html[:_tm.start()] + doc_html[_tm.end():]

        if len((doc_html or "").strip()) < 500 or not re.search(r"<(?:p|h[1-6])\b", doc_html or "", re.I):
            gutenberg_body = _convert_text_to_gutenberg(doc_text)
        else:
            gutenberg_body = _convert_google_html_to_gutenberg(_body_html, img_map)

        if takeaways:
            takeaways_html = "\n".join(f"<li>{b}</li>" for b in takeaways)
            takeaways_block = (
                f'<h2 class="wp-block-heading">Key Takeaways</h2>\n<ul class="wp-block-list">\n{takeaways_html}\n</ul>'
            )
            _first_p = re.search(r"</p>", gutenberg_body)
            if _first_p:
                at = _first_p.end()
                content = gutenberg_body[:at] + "\n\n" + takeaways_block + "\n\n" + gutenberg_body[at:]
            else:
                content = takeaways_block + "\n\n" + gutenberg_body
        else:
            content = gutenberg_body

    best_kp, best_meta = _seo_best(content, seo_data["keyphrases"], seo_data["metas"], title=title)

    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    wp_body = {
        "title": title,
        "content": content,
        "status": "draft",
        "slug": slug,
    }

    # Articles + Blogs post to /posts (standard post); Events post to /event
    wp_endpoint = "/posts" if (is_article or is_blog) else "/event"
    res = await _wp("POST", wp_endpoint, json_body=wp_body)
    if not res or not isinstance(res, dict) or "id" not in res:
        raise HTTPException(502, f"WordPress {wp_endpoint} creation returned invalid response")

    post_id = res["id"]
    permalink = res.get("link", "")
    _o = urllib.parse.urlparse(permalink)
    link = f"{_o.scheme}://{_o.netloc}/wp-admin/post.php?post={post_id}&action=edit" if _o.netloc else permalink

    registry_status = None
    if kind == "event":
        try:
            slug_key = _covered_slug(title) or _covered_slug(slug)
            row_num = await _pipeline_find_row(slug_key)
            today_iso = datetime.now().strftime("%Y-%m-%d")
            if row_num:
                await _sheet_update_cell(COVERED_OURS_SHEET, "Pipeline", row_num, "F", "DRAFT")
            else:
                await _sheet_append_rows(
                    COVERED_OURS_SHEET,
                    "Pipeline",
                    [[today_iso, title, slug_key, "", permalink, "DRAFT"]],
                )
            registry_status = "draft-logged"
        except Exception as e:
            registry_status = f"skipped: {e}"

    ret = {
        "wp_id": post_id,
        "link": link,
        "status": "draft",
        "kind": kind,
        "title": title,
        "location": parsed["location"],
        "dates_raw": parsed["dates_raw"],
        "start_date": parsed["start_date"],
        "end_date": parsed["end_date"],
        "seo_model": seo_model,
        "images_uploaded": images_uploaded,
        "seo": {
            "focus_keyphrase": best_kp,
            "meta_description": best_meta,
            "keyphrases": seo_data["keyphrases"],
            "metas": seo_data["metas"],
            "key_takeaways": seo_data["ai_b"],
        },
    }
    if registry_status is not None:
        ret["registry"] = registry_status
    return ret


@router.post("/api/thailandnow/events/wp-publish")
async def publish_event_to_wp(payload: dict = Body(default={})):
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid JSON payload")

    title = (payload.get("title") or "").strip()
    body_text = (payload.get("body") or "").strip()

    if not title or not body_text:
        raise HTTPException(400, "missing title or body")

    category = (payload.get("category") or "Events").strip()
    start_date = (payload.get("start_date") or "").strip()
    end_date = (payload.get("end_date") or "").strip()
    location = (payload.get("location") or "").strip()
    source_url = (payload.get("url") or "").strip()
    urls = payload.get("urls") or ([source_url] if source_url else [])

    is_publish = bool(payload.get("publish", False))
    post_status = "publish" if is_publish else "draft"

    seo_data, seo_model = await _generate_event_seo(title, body_text, category)

    takeaways_html = "\n".join(f"<li>{b}</li>" for b in seo_data["ai_b"])

    if start_date and end_date and start_date != end_date:
        dates_str = f"{start_date} to {end_date}"
    else:
        dates_str = start_date or end_date or "TBA"

    source_links = ", ".join(f'<a href="{u}" target="_blank" rel="noopener">{u}</a>' for u in urls if u)
    source_html = f"<p><strong>Source:</strong> {source_links}</p>" if source_links else ""

    venue_html = f"<p><strong>Venue:</strong> {location}</p>\n" if location else ""

    content = (
        f'<div class="ai-summary">\n<p><strong>AI Summary:</strong> {seo_data["ai_a"]}</p>\n</div>\n\n'
        f'{body_text}\n\n'
        f'<div class="key-takeaways">\n<h3>Key Takeaways</h3>\n<ul>\n{takeaways_html}\n</ul>\n</div>\n\n'
        f'<div class="event-details">\n'
        f'<p><strong>Dates:</strong> {dates_str}</p>\n'
        f'{venue_html}'
        f'{source_html}\n'
        f'</div>\n\n'
        f'<p class="hashtags">{seo_data["hashtags"]}</p>'
    )

    wp_body: dict = {
        "title": title,
        "content": content,
        "status": post_status,
    }

    slug = (payload.get("slug") or "").strip()
    if slug:
        wp_body["slug"] = slug
    if payload.get("categories"):
        wp_body["categories"] = payload["categories"]
    if payload.get("tags"):
        wp_body["tags"] = payload["tags"]

    res = await _wp("POST", "/event", json_body=wp_body)
    if not res or not isinstance(res, dict) or "id" not in res:
        raise HTTPException(502, "WordPress event creation returned invalid response")

    post_id = res["id"]
    permalink = res.get("link", "")
    _o = urllib.parse.urlparse(permalink)
    link = f"{_o.scheme}://{_o.netloc}/wp-admin/post.php?post={post_id}&action=edit" if _o.netloc else permalink

    try:
        updated_post = await _wp("GET", f"/event/{post_id}")
        if updated_post and isinstance(updated_post, dict):
            link = updated_post.get("link") or link
    except Exception:
        pass

    return {
        "wp_id": post_id,
        "link": link,
        "status": post_status,
        "seo_model": seo_model,
        "seo": {
            "keyphrases": seo_data["keyphrases"],
            "metas": seo_data["metas"],
            "hashtags": seo_data["hashtags"],
            "ai_a": seo_data["ai_a"],
            "ai_b": seo_data["ai_b"],
        },
    }


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
    # _load_gem trap: an intro that mentions the literal '## Role & Purpose'
    # inline must NOT make a first-match find() grab the wrong spot (Somatic port
    # bug 2026-07-29). Line-anchored match + frontmatter strip = the real heading wins.
    _trap = ("---\ntitle: x\n---\n"
             "Notes: see the `## Role & Purpose` heading below for the real prompt.\n\n"
             "## Role & Purpose\nThis is the real body.\n\n---\n")
    _tb = _extract_gem_body(_trap)
    assert _tb.startswith("## Role & Purpose") and "real body" in _tb, "gem intro-trap regression"
    # ARCHIVE Stage 2: Q&A gem loads + extracts the source-only rules.
    agem = _load_gem(_archive_gem_path())
    assert agem.startswith("## Role & Purpose"), "archive gem extraction wrong"
    assert "does not list" in agem.replace("\n", " "), "archive gem missing no-fabricate line"
    assert "per <doc name>" in agem, "archive gem missing citation rule"
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
    # SEO HEALTH (S1): classify / parse / orphan graph / suggest.
    sh = "thailandnow.in.th"
    assert _seo_classify("/arts/x/", sh) == "internal"
    assert _seo_classify("https://www.thailandnow.in.th/y", sh) == "internal"
    assert _seo_classify("https://thailandnow.in.th/y", sh) == "internal"  # non-www matches
    assert _seo_classify("//thailandnow.in.th/y", sh) == "internal"        # schemeless
    assert _seo_classify("foo.html", sh) == "internal"                     # bare relative
    assert _seo_classify("https://youtube.com/w", sh) == "external"
    for u in ("#frag", "mailto:a@b.com", "tel:+66", "javascript:void(0)",
              "data:image/gif;base64,AAA", ""):
        assert _seo_classify(u, sh) is None, u
    assert _seo_path("https://www.thailandnow.in.th/arts/x/?q=1#t") == "/arts/x"
    assert _seo_img_base("https://x/khon-09-1024x683.jpg") == "https://x/khon-09.jpg"
    assert _seo_img_base("https://x/khon-09-e1596123456.jpg") == "https://x/khon-09.jpg", "edit-variant strip"
    assert _seo_img_has_ext("https://x/y.jpg") and not _seo_img_has_ext("https://x/edit-no-ext")
    # Slice 2: _seo_strip_target tag matching and stripping
    st_a, st_c, st_b, st_af = _seo_strip_target('<p>Hello <a href="https://dead.link/x">Dead Link Text</a> here.</p>', "link", "https://dead.link/x")
    assert st_c == 1, st_c
    assert st_a == "<p>Hello Dead Link Text here.</p>", st_a
    assert "Dead Link Text" in st_af, st_af
    st_img, st_img_c, _, _ = _seo_strip_target('<p>Logo: <img src="https://dead.img/a.jpg" alt="test"> image</p>', "image", "https://dead.img/a.jpg")
    assert st_img_c == 1, st_img_c
    assert st_img == "<p>Logo:  image</p>", st_img
    # parser: data: placeholder skipped; real URL from data-src + srcset captured
    ex = _seo_extract(
        '<a href="/good/">g</a><a href="https://youtube.com/w">y</a><a href="#skip">s</a>'
        '<img src="data:image/gif;base64,AAA" data-src="https://www.thailandnow.in.th/wp-content/uploads/a.jpg">'
        '<img src="https://www.thailandnow.in.th/wp-content/uploads/b.jpg"'
        ' srcset="https://www.thailandnow.in.th/wp-content/uploads/b-768.jpg 768w">',
        sh,
    )
    assert ex["internal_links"] == ["/good/"], ex["internal_links"]
    assert ex["external_links"] == ["https://youtube.com/w"], ex["external_links"]
    assert "data:image/gif" not in str(ex["internal_imgs"]) + str(ex["external_imgs"])
    assert any(u.endswith("/a.jpg") for u in ex["internal_imgs"]), ex["internal_imgs"]
    assert any(u.endswith("/b-768.jpg") for u in ex["internal_imgs"]), ex["internal_imgs"]
    # internal report: whitelisted category link excluded; sized image matched to media
    # original; extensionless ref skipped; orphan among post AND event.
    posts = [
        {"id": 1, "link": "https://www.thailandnow.in.th/p1/", "title": {"rendered": "Post One"},
         "content": {"rendered": '<a href="/p2/">two</a><a href="/gone/">gone</a>'
                     '<a href="/current-affairs/">cat</a>'
                     '<img src="https://www.thailandnow.in.th/wp-content/uploads/img1-1024x683.jpg">'
                     '<img src="https://www.thailandnow.in.th/wp-content/uploads/img-broken.jpg">'
                     '<img src="https://www.thailandnow.in.th/wp-content/uploads/edit-no-ext">'}},
        {"id": 2, "link": "https://www.thailandnow.in.th/p2/", "title": {"rendered": "Post Two"},
         "content": {"rendered": ""}},
        {"id": 3, "link": "https://www.thailandnow.in.th/p3/", "title": {"rendered": "Post Three"},
         "content": {"rendered": '<a href="https://youtube.com/w">yt</a>'}},
    ]
    pages = [{"id": 7, "link": "https://www.thailandnow.in.th/about/", "title": {"rendered": "About"},
              "content": {"rendered": '<a href="/p1/">p1</a>'}}]
    events = [{"id": 9, "link": "https://www.thailandnow.in.th/event/e1/", "title": {"rendered": "Event One"},
               "content": {"rendered": ""}}]
    other_cpts = [{"id": 12, "link": "https://www.thailandnow.in.th/interviews/khon/", "title": {"rendered": "Interview Khon"},
                   "content": {"rendered": '<a href="Facebook/MangledLink">mangled</a>'}}]
    rep = _seo_internal_report(
        posts, pages, events, other_cpts,
        {"https://www.thailandnow.in.th/wp-content/uploads/img1.jpg"},  # original → matches the -1024x683 crop
        {"/current-affairs"}, sh)
    assert rep["post_count"] == 3 and rep["page_count"] == 1 and rep["event_count"] == 1 and rep["other_cpt_count"] == 1, rep
    assert rep["valid_paths"] == 7, rep["valid_paths"]  # /p1,/p2,/p3,/about,/event/e1,/interviews/khon + /current-affairs
    # Fix 1: _seo_internal_report now emits candidates (not broken); flow HTTP-verifies them.
    cands = rep["internal_link_candidates"]
    assert len(cands) == 2, cands  # /gone/ + Facebook/MangledLink
    assert cands[0]["to"] == "/gone", cands
    assert cands[0]["from_id"] == 1, "from_id plumbed"   # /gone/ lives in p1
    assert cands[1]["to"] == "/Facebook/MangledLink" or cands[1]["to"] == "Facebook/MangledLink", cands
    # Fix 1: _seo_resolve_href resolves bare relative to absolute
    so = "https://www.thailandnow.in.th"
    assert _seo_resolve_href("/gone/", so) == "https://www.thailandnow.in.th/gone/"
    assert _seo_resolve_href("Facebook/Mangled", so) == "https://www.thailandnow.in.th/Facebook/Mangled"
    assert _seo_resolve_href("//example.com/x", so) == "https://example.com/x"
    assert _seo_resolve_href("https://example.com/y", so) == "https://example.com/y"
    assert _seo_resolve_href("", so) == ""
    # Fix 2: _seo_internal_link_reason — mangled-external regex (no ^ anchors) + site-host exclusion
    assert _seo_internal_link_reason("Facebook/MangledLink", sh) == "likely a broken external link (missing scheme)"
    assert _seo_internal_link_reason("Source: Facebook/ASEANParaGamesThailand2025", sh) == "likely a broken external link (missing scheme)"
    assert _seo_internal_link_reason("/interviews/", sh) == "not a published page"  # no mangled host
    assert _seo_internal_link_reason("https://www.thailandnow.in.th/gone/", sh) == "not a published page"  # own host → not mangled
    assert len(rep["image_candidates"]) == 1, rep["image_candidates"]  # img-broken only (img1 crop matches)
    assert rep["image_candidates"][0]["src"].endswith("img-broken.jpg"), rep["image_candidates"]
    assert rep["image_candidates"][0]["from_id"] == 1, "image candidate from_id plumbed"
    assert len(rep["orphans"]) == 3, rep["orphans"]  # post3 + event1 + interview12
    assert {o["id"] for o in rep["orphans"]} == {3, 9, 12}, "orphan id plumbed"
    assert rep["external_links"] == ["https://youtube.com/w"], rep["external_links"]
    assert rep["ext_sources"]["https://youtube.com/w"][0]["from_id"] == 3, "external link per-post attribution"
    # suggest: token overlap ranks + filters
    sugg = _seo_suggest("Khon epic festival", [
        ("https://www.thailandnow.in.th/khon/", "Record youth turnout for Khon epic"),
        ("https://www.thailandnow.in.th/songkran/", "Songkran water festival"),
        ("https://www.thailandnow.in.th/food/", "Best street food"),
    ], n=2)
    assert sugg and sugg[0]["link"].endswith("/khon/") and len(sugg) == 2, sugg
    # Orphan un-orphan flow: valid regions / anchor phrases / candidates / insert.
    vr_html = ('<p>eat <a href="/x/">Khon Kaen here</a></p>'
               '<h2>Khon Kaen head</h2><p>Khon Kaen plain</p>')
    vr = _seo_valid_regions(vr_html)

    def _vr_ok(i: int) -> bool:
        return any(a <= i < b for a, b in vr)

    assert not _vr_ok(vr_html.find("Khon Kaen here")), "inside <a> must be invalid"
    assert not _vr_ok(vr_html.find("Khon Kaen head")), "inside <h2> must be invalid"
    assert _vr_ok(vr_html.find("Khon Kaen plain")), "plain paragraph must be valid"
    ap = _seo_anchor_phrases("Khon Kaen Street Food Guide")
    assert ap and ap[0] == "Khon Kaen Street Food Guide", ap
    assert ap == sorted(ap, key=len, reverse=True) and all(len(p) >= 4 for p in ap), ap
    assert _seo_anchor_phrases("How to") == [], "stopword-only phrase dropped"
    ap_t = _seo_anchor_phrases("เที่ยวขอนแก่น 3 วัน")
    assert ap_t and ap_t[0] == "เที่ยวขอนแก่น 3 วัน", ap_t  # full Thai title survives
    host = '<p>Visit Khon Kaen for food.</p><p><a href="/k/">Khon Kaen guide</a></p>'
    ac = _seo_anchor_candidates("Khon Kaen Street Food Guide", host)
    assert ac and all(c["count"] >= 1 and c["snippet"] for c in ac), ac
    assert ac == sorted(ac, key=lambda c: len(c["phrase"]), reverse=True), ac
    kc = [c for c in ac if c["phrase"] == "Khon Kaen"]
    assert kc and kc[0]["count"] == 1, ac  # the <a>-wrapped one doesn't count
    assert len(_seo_anchor_candidates("Khon Kaen Street Food Guide", host, cap=1)) == 1, "cap honored"
    ih, im, ib, ia = _seo_insert_link("<p>Khon kaen is nice.</p>", "Khon Kaen", "/orphan/")
    assert im == 1 and '<a href="/orphan/">Khon kaen</a>' in ih, (im, ih)  # casing preserved
    assert ib and ia and "Khon kaen" in ib, (ib, ia)
    ih2, im2, _, _ = _seo_insert_link(
        '<p><a href="/k/">Khon Kaen guide</a> and Khon Kaen plain.</p>', "Khon Kaen", "/o/")
    assert im2 == 1 and '<a href="/o/">Khon Kaen</a> plain' in ih2, (im2, ih2)
    assert ih2.count("<a ") == 2, ih2  # the <a>-wrapped occurrence stays untouched
    iz, izm, _, _ = _seo_insert_link("<p>nothing here</p>", "Khon Kaen", "/o/")
    assert izm == 0 and iz == "<p>nothing here</p>", (izm, iz)
    # attribute interiors are NEVER anchor spots (validator blocker: wrapping
    # inside alt="..." would corrupt markup written to WP)
    attr_html = '<img src="/x.jpg" alt="Khon Kaen Street Food"> <p>Khon Kaen rocks</p>'
    attr_regions = _seo_valid_regions(attr_html)
    assert not any(a <= attr_html.find("Street Food") < b for a, b in attr_regions), \
        "inside an attribute must be invalid"
    ah, am, _, _ = _seo_insert_link(attr_html, "Khon Kaen Street Food", "/o/")
    assert am == 0 and ah == attr_html, f"must not wrap inside a tag: {(am, ah)}"
    attr_host = '<img src="/x.jpg" alt="Khon Kaen Street Food">'
    assert _seo_anchor_candidates("Khon Kaen Street Food Guide", attr_host) == [], \
        "attribute-only occurrences are not candidates"
    gt_html = '<img alt="a > b" src="/x.jpg"><p>Khon Kaen rocks</p>'
    gt_regions = _seo_valid_regions(gt_html)
    assert not any(a <= gt_html.find("src=") < b for a, b in gt_regions), \
        "tail of a tag with quoted '>' must stay invalid"
    gh, gm, _, _ = _seo_insert_link(gt_html, "b\" src=\"/x.jpg", "/o/")
    assert gm == 0, "no wrapping inside a quoted-'>' tag tail"
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
    assert not _archive_content_tokens("do we have any events lined up for 2026?"), "presence → no content tokens"
    assert not _archive_content_tokens("show me what's coming up"), "presence"
    assert not _archive_content_tokens("2026"), "bare year → presence (year stripped)"
    assert _archive_content_tokens("Songkran"), "bare event name → has content tokens"
    assert _archive_content_tokens("do we have Songkran?"), "specific → has content tokens (title search)"
    assert _archive_content_tokens("do we have Songkran lined up?") == {"songkran"}, _archive_content_tokens("do we have Songkran lined up?")
    assert _archive_is_detail("when is Songkran?") is True
    assert _archive_is_detail("where is it held") is True
    assert _archive_is_detail("Songkran") is False, "bare event name not detail"
    # recursion union: dedupe by id, sort newest-first
    _dupe_a = [{"id":"1","name":"A","modifiedTime":"2026-07-01T00:00:00Z"},
               {"id":"1","name":"A","modifiedTime":"2026-07-01T00:00:00Z"}]   # same doc, 2 parents
    _dupe_b = [{"id":"2","name":"B","modifiedTime":"2026-07-05T00:00:00Z"}]
    _u = _archive_union([_dupe_a, _dupe_b])
    assert len(_u) == 2, "dedupe by id failed"
    assert _u[0]["id"] == "2", "newest-first sort failed (B is later than A)"
    # STORY SCOUT — news extraction + gem resolves
    _nmd = "# Bangkok rail extension opens December 2026\n\nThe new rail line links Suvarnabhumi to the city center, cutting taxi costs for arrivals.\n\nMore text here."
    _n = _extract_news(_nmd, "https://www.thairath.co.th/news/foreign/123")
    assert _n and _n["title"].startswith("Bangkok rail"), "news title parse failed"
    assert "rail" in _n["snippet"].lower() and len(_n["snippet"]) <= 300, "news snippet failed"
    assert _n["lang"] == "en" and _n["source"] == "thairath.co.th", "news meta failed"
    # bot-check pages drop; Jina metadata prefixes strip from title + snippet
    assert _extract_news("# Just a moment...\n\nChecking your browser before continuing.", "https://x.example/cf") is None, "bot-check page should drop"
    _jm = _extract_news("Title: Real Headline\nURL Source: https://x.example\nMarkdown Content:\n\nFirst paragraph here.", "https://x.example")
    assert _jm and _jm["title"] == "Real Headline" and "first paragraph" in _jm["snippet"].lower(), "jina meta not stripped / snippet"
    _pt = _extract_news("# Headline\n\nPublished Time: 2026-07-15T10:00:00Z\n\nBody text here.", "https://x.example")
    assert _pt and _pt["date"] == "2026-07-15", "published-time date not parsed"
    assert _extract_news("# 403 Forbidden\n\nWarning: Target URL returned error 403: Forbidden.", "https://x.example/403") is None, "jina error page should drop"
    # scout domain exclusion + strict date-range bounds
    assert _scout_domain_excluded("thailandnow.in.th") and _scout_domain_excluded("blog.thailandnow.in.th"), "own-site exclude failed"
    assert not _scout_domain_excluded("bangkokpost.com"), "false domain exclude"
    assert _scout_date_in_range("2026-07-15", "2026-06-27", "2026-07-27"), "in-range date should keep"
    assert not _scout_date_in_range("2025-04-22", "2026-06-27", "2026-07-27"), "stale date should drop"
    assert not _scout_date_in_range("2026-12-01", "2026-06-27", "2026-07-27"), "future date should drop"
    assert not _scout_date_in_range("", "2026-06-27", "2026-07-27"), "undated not in-range"
    # markdown link/image syntax stripped from titles
    assert _extract_news("# [Thairath Website](https://thairath.co.th)\n\nBody text.", "https://thairath.co.th").get("title") == "Thairath Website", "markdown link in title not stripped"
    _sgp = _scout_gem_path()
    assert _sgp.name == "story-scout-pitch.md" and _sgp.exists(), "scout gem path missing"
    assert "headline_en" in _load_gem(_sgp), "scout gem extract failed"
    # rerank mapping assert
    _rr_cands = [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}]
    _rr_out = [{"idx": 0, "score": 8, "keep": True}, {"idx": 1, "score": 5, "keep": False}, {"idx": 2, "score": 9, "keep": True}]
    _rr_res = _scout_apply_rerank(_rr_cands, _rr_out)
    assert [c["id"] for c in _rr_res] == [2, 0, 3], "rerank apply mapping failed"
    # stock filter & keyless check asserts
    assert _scout_filter_stock([{"h": 1080, "w": 1920, "url": "a"}, {"h": 720, "w": 1280, "url": "b"}]) == [{"h": 1080, "w": 1920, "url": "a"}], "stock 1080p filter failed"
    import unittest.mock
    with unittest.mock.patch.dict(os.environ, {"PEXELS_API_KEY": "", "PIXABAY_API_KEY": ""}):
        assert asyncio.run(_pexels_photos("test")) == [] and asyncio.run(_pixabay_photos("test")) == [], "keyless stock helpers should return []"
    print("OK archive + story scout: tokenize + title score + presence/detail classify + recursive union + news extract")

    # WPOP: doc title extraction, gutenberg conversion, text fallback, seo parsing, best seo
    assert _extract_doc_title("<h1>Sun-Dried Squid Festival</h1><p>Body</p>") == "Sun-Dried Squid Festival"
    assert _extract_doc_title("<p>No heading here</p>") == ""
    _fb_text = "Facebook\n\nSun-Dried Squid Festival\n\nCome and enjoy squid."
    _gt = _convert_text_to_gutenberg(_fb_text)
    assert '<p class="wp-block-paragraph">Sun-Dried Squid Festival</p>' in _gt
    assert "Facebook" not in _gt
    _clean_html = _clean_gutenberg_attributes('<span style="color:red"><a href="https://example.com" class="wp-block-x">Link</a></span>')
    assert "<span" not in _clean_html and 'class="wp-block-x"' in _clean_html and 'target="_blank"' in _clean_html
    _gdoc_html = '<body class="doc-content"><h1>Main Event</h1><p>First paragraph.</p><img src="img1.jpg"></body>'
    _conv = _convert_google_html_to_gutenberg(_gdoc_html, {"img1.jpg": {"id": 101, "source_url": "https://site/img1.jpg", "alt": "Squid"}})
    assert '<h2 class="wp-block-heading">Main Event</h2>' in _conv
    assert '<p class="wp-block-paragraph">First paragraph.</p>' in _conv
    assert '<figure class="wp-block-image size-large"><img src="https://site/img1.jpg" alt="Squid"></figure>' in _conv
    _best_k, _best_m = _seo_best("<p>Sun-Dried Squid Festival is amazing.</p>", ["Squid Festival", "Thailand Travel"], ["Enjoy Sun-Dried Squid Festival in Rayong (125 chars)"], title="Sun-Dried Squid Festival")
    assert _best_k == "Squid Festival"
    assert _best_m.startswith("Enjoy Sun-Dried")
    _seo_raw = (
        "## Focus Keyphrases (5 options)\n"
        "1. Squid Festival - Priority: High - Reason: Popular search\n"
        "2. Rayong Food - Priority: Med - Reason: Location\n\n"
        "## Meta Descriptions (5 options)\n"
        "1. Enjoy the annual Sun-Dried Squid Festival in Rayong this August with fresh seafood. (128)\n\n"
        "## Related Hashtags\n"
        "#SquidFestival #Rayong #ThailandEvents #Seafood #ThailandNOW\n\n"
        "## AI SEO Block\n"
        "### Version A — AI Summary\n"
        "The Sun-Dried Squid Festival takes place in Rayong on August 15, 2026, offering fresh seafood from local fishermen.\n\n"
        "### Version B — Key Takeaways\n"
        "- The Sun-Dried Squid Festival takes place in Rayong in August 2026.\n"
        "- Visitors can taste fresh squid dried under natural sunlight on the beach.\n"
        "- Local fishermen demonstrate traditional squid preservation techniques.\n"
        "- The event supports community tourism and coastal livelihoods in Rayong.\n"
        "- Admission is free for all international and local visitors.\n"
    )
    _parsed_seo = _parse_gemini_seo(_seo_raw)
    assert _parsed_seo and len(_parsed_seo["keyphrases"]) == 2
    assert _parsed_seo["keyphrases"][0] == "Squid Festival"
    assert _parsed_seo["metas"][0].startswith("Enjoy the annual")
    assert "#SquidFestival" in _parsed_seo["hashtags"]
    assert "Sun-Dried Squid Festival takes place" in _parsed_seo["ai_a"]
    assert len(_parsed_seo["ai_b"]) == 5
    _seo_gem_prompt = _get_seo_gem_system_prompt()
    assert "## Role & Purpose" in _seo_gem_prompt or "Role & Purpose" in _seo_gem_prompt
    print("OK WPOP: doc title + gutenberg conversion + text fallback + seo parse + seo best")

    # TRAFFIC OP: day numbering, date caps, paste lines, proposed writes
    assert _traffic_day("2025-12-05", "2025-12-05") == 1, "contract start day must be 1"
    assert _traffic_day("2026-08-21", "2025-12-05") == 260
    assert _traffic_dates("2026-08-01", "2026-08-03") == ["2026-08-01", "2026-08-02", "2026-08-03"]
    assert len(_traffic_dates("2026-01-01", "2026-04-02")) == 92, "91-day span inclusive = 92 dates (max)"
    try:
        _traffic_dates("2026-01-01", "2026-04-03")  # 93 dates → reject
        raise SystemExit("traffic dates cap failed")
    except ValueError:
        pass
    _tl = _traffic_text_lines([
        {"date": "2026-08-21", "day": 260, "total": 232299, "daily": 1021, "target": 211258},
        {"date": "2025-12-05", "day": 1, "total": 4211, "daily": 4211, "target": None},
        {"date": "2026-08-20", "day": 259, "total": 231278, "daily": 0, "target": 210000},
    ]).splitlines()
    assert _tl[0] == ("Aug 21 · Day 260 · Total 232,299 · Daily +1,021 · "
                      "Target 211,258 (Δ +21,041)"), _tl[0]
    assert _tl[1] == "Dec 5 · Day 1 · Total 4,211 · Daily +4,211", _tl[1]  # missing target omitted
    assert _tl[2] == ("Aug 20 · Day 259 · Total 231,278 · Daily +0 · "
                      "Target 210,000 (Δ +21,278)"), _tl[2]  # zero daily → +0
    # proposed writes — REAL sheet layout: phase-metadata rows above a header
    # found by detection (A empty, B serial date, C Day, D Target formula,
    # E Actual plain, F Daily formula). Cases: (a) overwrite w/ plain F,
    # (b) formula D/F echoed untouched, (c) stale +0 daily corrected,
    # (d) past-last-row append w/ shifted D/F formulas, (e) serial-date clash
    # w/ Day mismatch → warning + skip, (f) year-boundary append.
    assert _traffic_serial_to_iso(45996) == "2025-12-05", "Day-1 serial anchor"
    assert _traffic_iso_to_serial("2025-12-05") == 45996
    assert _traffic_serial_to_iso(_traffic_iso_to_serial("2026-08-21")) == "2026-08-21"
    _sheet = [
        ["", "Phase 1", "Days", 45996, 46225, 900000, ""],   # phase metadata — ignored
        ["", "Phase 2", "Days", 46226, 46361, 1800000, ""],
        ["", "", "", "", "", "", ""],
        ["", "", "", "", "", "", ""],
        ["", "Date", "Day", "Target Traffic", "Actual Traffic", "Daily Traffic"],  # header, row 5
        ["", 46254, 259, 210000, 231278, 1212],             # row 6 (Aug 20)
        ["", 46255, 260, "=D6+$I$6", 232278, 0],            # row 7: (a)+(c) stale 0 daily, plain F
        ["", 46256, 261, "=D7+$I$6", 232950, "=E8-E7"],     # row 8: (b) formula D+F
    ]
    _hr, _cols = _traffic_columns(_sheet)
    assert _hr == 5 and _cols == {"date": 1, "day": 2, "target": 3, "actual": 4, "daily": 5}, (_hr, _cols)
    _ga = {"2026-08-21": 233320, "2026-08-22": 234000, "2026-08-23": 235000}
    _pw = _traffic_proposed_writes(_sheet, ["2026-08-21", "2026-08-22", "2026-08-23"],
                                   _ga, "2025-12-05", _hr, _cols)
    _w21 = next(w for w in _pw["writes"] if w["date"] == "2026-08-21")
    assert _w21["row"] == 7 and _w21["actual_new"] == 233320 and _w21["daily_old"] == 0
    assert _w21["daily_new"] == 233320 - 231278 == 2042, _w21  # (c) stale corrected
    assert _w21["target_old"] == "=D6+$I$6", _w21  # D formula echoed verbatim
    _w22 = next(w for w in _pw["writes"] if w["date"] == "2026-08-22")
    assert _w22["daily_new"] is None and _w22["daily_is_formula"], _w22  # (b)
    assert _w22["daily_old"] == "=E8-E7", _w22  # F formula preserved for the full-row write
    assert len(_pw["appends"]) == 1, _pw  # (d)
    _ap = _pw["appends"][0]
    assert _ap["date"] == "2026-08-23" and _ap["day"] == 262, _ap
    assert _ap["target"] == "=D8+$I$6", _ap  # D formula shifted to the new row 9
    assert _ap["daily_new"] == "=E9-E8", _ap  # F formula shifted likewise
    assert _ap["actual_new"] == 235000, _ap
    assert _pw["warnings"] == [], _pw
    # (e) serial-date clash: row holds the date but its Day disagrees → skip
    _pe_sheet = [
        ["", "Date", "Day", "Target Traffic", "Actual Traffic", "Daily Traffic"],
        ["", 46257, 300, 1, 1, 1],  # holds 2026-08-23 but Day says 300
    ]
    _peh, _pec = _traffic_columns(_pe_sheet)
    _pe = _traffic_proposed_writes(_pe_sheet, ["2026-08-23"], {"2026-08-23": 5},
                                   "2025-12-05", _peh, _pec)
    assert _pe["writes"] == [] and _pe["appends"] == [], _pe
    assert any("disagrees" in w for w in _pe["warnings"]), _pe  # (e)
    # Day-1 anchor sanity: sheet's Day-1 serial must equal contract start's
    _anchor = [
        ["", "Phase 1", "Days", 45996, 46361, 900000, ""],
        ["", "Date", "Day", "Target Traffic", "Actual Traffic", "Daily Traffic"],
        ["", 46000, 1, 1000, 500, "=E3"],  # wrong serial for Day 1
    ]
    _ah, _ac = _traffic_columns(_anchor)
    _pan = _traffic_proposed_writes(_anchor, [], {}, "2025-12-05", _ah, _ac)
    assert any("anchor mismatch" in w for w in _pan["warnings"]), _pan
    # (f) year-boundary: day 366 appends even though the date shares month+day
    # with the day-1 row a year earlier (serial dates can never clash)
    _yb_sheet = [["", "Date", "Day", "Target Traffic", "Actual Traffic", "Daily Traffic"]] + [
        ["", 45996 + k - 1, k, 1, k * 10, 10] for k in range(1, 366)
    ]
    _ybh, _ybc = _traffic_columns(_yb_sheet)
    _yb = _traffic_proposed_writes(_yb_sheet, ["2026-12-05"], {"2026-12-05": 9999},
                                   "2025-12-05", _ybh, _ybc)
    assert len(_yb["appends"]) == 1 and _yb["appends"][0]["day"] == 366, _yb
    assert _yb["warnings"] == [], _yb
    print("OK traffic: day/dates/text/serial+header detect/writes+appends+warnings")
