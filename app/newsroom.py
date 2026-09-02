"""Newsroom panel — thin subprocess wrapper around the newsroom skill scripts
(``queue.py`` + ``nl_append.py``) in the skill-library repo (synced to ~/.claude/skills/).

Does NOT reimplement fetch/dedup/append logic — the CLI scripts are the
contract (they import newstank + the google-workspace MCP creds directly).
Mirrors the other panel backends: argv **lists** via
``asyncio.create_subprocess_exec`` (never shell), errors surfaced as
HTTPException with the script's stderr tail.

Ported from Somatic's ``app/newsroom.py`` (SomaticRailjack ``18ef2ff``,
originally a GLM agent-x build from RAILJACK-PANEL-BRIEF.md), restated in
Railjack's APIRouter idiom.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from . import zai
from .name_check import _HONORIFICS, check_rewritten, load_registry
from .style_check import check_style

router = APIRouter()

SCRIPTS = Path.home() / ".claude" / "skills" / "newsroom" / "scripts"
QUEUE = SCRIPTS / "queue.py"
APPEND = SCRIPTS / "nl_append.py"
RADIO = SCRIPTS / "radio.py"
NEWSLINE = SCRIPTS / "newsline.py"
NEWSLINE_REPORTS = Path(__file__).parent / "newsline_reports.py" if (Path(__file__).parent / "newsline_reports.py").exists() else SCRIPTS / "newsline_reports.py"
# The Rules Gem drives REWRITE (news-producer prompt → two-layer broadcast
# script). ~/Gems is the office canonical copy; home has no ~/Gems, so the
# vault-synced gem is the source here — _gem_text falls through to it.
BEN_GEM = Path(__file__).parent / "gems" / "radio-news-rewrite.md"
SEO_GEM = Path.home() / "Cephalon" / "10-knowledge" / "ai-workflow" / "gemini-gem-thailandnow-seo.md"
# Run via the system interpreter, not the scripts' shebang: the skill's deps live
# with the system python3, not Railjack's venv (the skill-library repo commits exec
# bits, but the venv-deps reason is the durable one — never invoke via shebang).
PY = "python3"


async def _run(argv: list[str], timeout: float = 90,
               stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "newsroom script timed out")
    return proc.returncode or 0, out, err


def _json(out: bytes):
    """Parse script stdout; a `_fatal` payload is the script reporting a
    user-visible failure (e.g. newstank auth) → 400 with its message."""
    data = json.loads(out)
    if isinstance(data, dict) and data.get("_fatal"):
        raise HTTPException(400, data["_fatal"])
    return data


def _fail(out: bytes, err: bytes) -> str:
    """Best error text for a nonzero exit. Scripts print their own reason as
    ``{"_fatal": ...}`` on STDOUT (e.g. 'run nl_auth.py once' when the Google
    creds are missing), so check stdout first — else SEND TO NL fails with a
    blank stderr and the button just reads 'script failed (no output)'."""
    try:
        d = json.loads(out)
        if isinstance(d, dict) and d.get("_fatal"):
            return d["_fatal"]
    except Exception:
        pass
    return err.decode(errors="replace")[-300:].strip() or "script failed (no output)"


async def _script(argv: list[str], timeout: float = 90, stdin: bytes | None = None):
    rc, out, err = await _run(argv, timeout=timeout, stdin=stdin)
    if rc != 0:
        raise HTTPException(502, _fail(out, err))
    return _json(out)


# ---------------------------------------------------------------- queue


@router.get("/api/newsroom/queue")
async def api_queue(date: str | None = None, author: str = "Chompatsorn"):
    """Undone stories for the day (default author Chompatsorn; `all` = every
    reporter). Delegates to `queue.py list` — dedup ledger applied there."""
    # --all: include already-sent rows (stamped `done`) — the QUEUE panel dims +
    # tags them "✓ SENT" instead of hiding; the ledger/mark path stays in queue.py.
    argv = [PY, str(QUEUE), "list", "--json", "--all", "--author", author]
    if date:
        argv += ["--date", date]
    return await _script(argv)


@router.get("/api/newsroom/story/{story_id}")
async def api_story(story_id: str):
    return await _script([PY, str(QUEUE), "show", story_id, "--json"])


@router.post("/api/newsroom/mark")
async def api_mark(body: dict = Body(...)):
    """Stamp ids into the machine-local ledger (~/.config/newsroom/) — the dedup."""
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(400, "ids required")
    argv = [PY, str(QUEUE), "mark"] + [str(i) for i in ids]
    if body.get("doc_id"):
        argv += ["--doc", body["doc_id"]]
    return await _script(argv)


@router.get("/api/newsroom/ledger")
async def api_ledger():
    return await _script([PY, str(QUEUE), "ledger", "--json"])


# ---------------------------------------------------------------- nl append


@router.post("/api/newsroom/append")
async def api_append(body: dict = Body(...)):
    """Append a finished script to the **bottom** of the day's NL & NWB rundown
    tab (default NL RUNDOWN; pass ``tab`` for AM/MID/EVE). `nl_append.py`
    resolves the doc (--today via Drive, or explicit --doc) and needs the
    google-workspace MCP OAuth creds on this machine."""
    text = _strip_inf(body.get("text", ""))
    if not text.strip():
        raise HTTPException(400, "text required")
    argv = [PY, str(APPEND)]
    tab = str(body.get("tab") or "NL").strip().upper()
    if tab != "NL":  # NL is the script default — only override for other tabs
        argv += ["--tab", tab]
    if body.get("doc_id"):
        argv += ["--doc", body["doc_id"]]
    else:
        argv.append("--today")
    argv += ["--text", text]
    return await _script(argv, timeout=60)


@router.post("/api/newsroom/fill")
async def api_fill(body: dict = Body(...)):
    """Replace story slot #N in the NL rundown tab.

    Body: {text, tab? (AM/MID/EVE/NL, default NL), slot (int), doc_id?}
    Calls ``nl_append.py fill --tab ... --slot N --today~~-doc ... --text ...``.
    400 if ``slot`` is missing or not an integer.
    """
    text = _strip_inf(body.get("text", ""))
    if not text.strip():
        raise HTTPException(400, "text required")
    slot = body.get("slot")
    try:
        slot = int(slot)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(400, "slot required (integer 1-12)")
    tab = body.get("tab") or "NL"
    argv = [PY, str(APPEND), "fill", "--tab", str(tab), "--slot", str(slot)]
    if body.get("doc_id"):
        argv += ["--doc", body["doc_id"]]
    else:
        argv.append("--today")
    argv += ["--text", text]
    return await _script(argv, timeout=60)


# ---------------------------------------------------------------- radio
# Monthly Drive batch generator (RADIO): copies the spreadsheet + per-day script
# templates into the pre-existing month folder. `radio.py` is the contract —
# this panel only builds argv and surfaces `_fatal` (→ 400) vs stderr (→ 502).



def _radio_argv(body: dict) -> list[str]:
    """Year/month required; sheet-name optional. Raises 400 on a missing
    year/month so the user gets a clear field error, not a 502."""
    year, month = body.get("year"), body.get("month")
    if year is None or month is None:
        raise HTTPException(400, "year and month required")
    argv = [PY, str(RADIO), "--year", str(year), "--month", str(month)]
    if body.get("sheet_name"):
        argv += ["--sheet-name", str(body["sheet_name"])]
    return argv


@router.post("/api/newsroom/radio/preview")
async def api_radio_preview(body: dict = Body(...)):
    """Dry-run plan (no writes): folder + counts + the to_create list."""
    return await _script(_radio_argv(body) + ["--dry-run"])


@router.post("/api/newsroom/radio/generate")
async def api_radio_generate(body: dict = Body(...)):
    """Real run — copies every planned file (~31 calls), hence the longer cap."""
    return await _script(_radio_argv(body), timeout=180)


@router.post("/api/newsroom/radio/fill")
async def api_radio_fill(body: dict = Body(...)):
    """Fill a slot in a daily Radio script doc.

    Body: {text, section (AM/MIDDAY/EVE), block (NATIONAL/GLOBAL/BUSINESS),
           slot (int), doc_id?}  — either doc_id (explicit target, from the
           panel's folder picker) or year/month/day (auto-resolve) is required.
    Calls ``radio.py fill --doc ...`` or ``--year ... --month ... --day ...``.
    400 on any missing required field.
    """
    required = ("section", "block", "slot")
    missing = [f for f in required if body.get(f) is None]
    if missing:
        raise HTTPException(400, "missing required fields: %s" % ", ".join(missing))
    doc_id = body.get("doc_id")
    if not doc_id:
        missing_ymd = [f for f in ("year", "month", "day") if body.get(f) is None]
        if missing_ymd:
            raise HTTPException(400, "doc_id, or year/month/day, required — missing %s" % ", ".join(missing_ymd))
    try:
        slot = int(body["slot"])
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "slot must be an integer: %s" % e)
    section = str(body["section"]).upper()
    block = str(body["block"]).upper()
    text = _strip_inf((body.get("text") or "").strip())
    if not text:
        raise HTTPException(400, "text required")
    argv = [
        PY, str(RADIO), "fill",
        "--section", section, "--block", block, "--slot", str(slot),
        "--text", text,
    ]
    if doc_id:
        argv += ["--doc", str(doc_id)]
    else:
        try:
            year = int(body["year"])
            month = int(body["month"])
            day = int(body["day"])
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "year/month/day must be integers: %s" % e)
        argv += ["--year", str(year), "--month", str(month), "--day", str(day)]
    return await _script(argv, timeout=60)


@router.post("/api/newsroom/radio/rundown")
async def api_radio_rundown(body: dict = Body(...)):
    """Auto-fill the monthly ``{YYYYMM}_Rundown`` tab for one day, then flip that
    tab's red "not done" cells to green.

    Body: {year, month, day, doc_id?, sheet_id?, dry_run?}. y/m/d are ALWAYS
    required — they locate the day tab ("01".."31") even when doc_id is given.
    ``dry_run`` returns the planned writes + red-cell count without writing.
    Reads 3 tabs of a doc + recolors, so it gets a longer cap than radio/fill.
    """
    try:
        ymd = [int(body[f]) for f in ("year", "month", "day")]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "year, month and day required (integers)")
    argv = [PY, str(RADIO), "rundown",
            "--year", str(ymd[0]), "--month", str(ymd[1]), "--day", str(ymd[2])]
    if body.get("doc_id"):
        argv += ["--doc", str(body["doc_id"])]
    if body.get("sheet_id"):
        argv += ["--sheet-id", str(body["sheet_id"])]
    if body.get("dry_run"):
        argv.append("--dry-run")
    return await _script(argv, timeout=180)



# ---------------------------------------------------------------- newsline
# Monthly Drive batch generator (NEWSLINE): one daily Google Doc per calendar
# day, copied from the fixed template + date-stamped, inside year/month folders
# it ensures under the Newsline home folder. `newsline.py` is the contract —
# this panel only builds argv and surfaces `_fatal` (→ 400) vs stderr (→ 502).


def _newsline_argv(body: dict) -> list[str]:
    """Year/month required (mirrors _radio_argv's 400 on a missing year/month).
    The command (generate/preview) is appended by the route so the argv lands as
    `newsline.py --year Y --month M generate|preview` — the flat argparse in
    newsline.py accepts flags-then-positional."""
    year, month = body.get("year"), body.get("month")
    if year is None or month is None:
        raise HTTPException(400, "year and month required")
    return [PY, str(NEWSLINE), "--year", str(year), "--month", str(month)]


@router.post("/api/newsroom/newsline/preview")
async def api_newsline_preview(body: dict = Body(...)):
    """Dry-run plan (no writes, no folder creation): folder flags + counts +
    the to_create day list."""
    return await _script(_newsline_argv(body) + ["preview"])


@router.post("/api/newsroom/newsline/generate")
async def api_newsline_generate(body: dict = Body(...)):
    """Real run — copies + stamps every planned daily doc (up to 31 days × 2
    calls: copy + batchUpdate), hence the generous 300s cap."""
    return await _script(_newsline_argv(body) + ["generate"], timeout=300)


# ---------------------------------------------------------------- newsline reports (Sub-tab 2: Monthly Report)
# Monthly NBT contractor work-report docs generator: Cover doc + Log doc per (period, date-range).
# `newsline_reports.py` is the contract — this panel builds argv and surfaces `_fatal` (→ 400).


def _newsline_reports_argv(body: dict) -> list[str]:
    """Period, start, and end required. Raises 400 on missing fields."""
    period = body.get("period")
    start = body.get("start")
    end = body.get("end")
    if period is None or not str(period).strip():
        raise HTTPException(400, "period required")
    if start is None or not str(start).strip():
        raise HTTPException(400, "start date required")
    if end is None or not str(end).strip():
        raise HTTPException(400, "end date required")
    return [
        PY, str(NEWSLINE_REPORTS),
        "--period", str(period).strip(),
        "--start", str(start).strip(),
        "--end", str(end).strip(),
    ]


@router.post("/api/newsroom/newsline-reports/preview")
async def api_newsline_reports_preview(body: dict = Body(...)):
    """Dry-run plan (no writes, pure compute): planned doc filenames + enumerated Mon-Fri Thai-numeral list."""
    return await _script(_newsline_reports_argv(body) + ["--dry-run", "preview"])


@router.post("/api/newsroom/newsline-reports/generate")
async def api_newsline_reports_generate(body: dict = Body(...)):
    """Real run — duplicates cover + log templates, fills contents, saves to FY folder. Idempotent."""
    return await _script(_newsline_reports_argv(body) + ["generate"], timeout=180)


def _newsline_report_autofill_argv(body: dict) -> list[str]:
    doc_id = body.get("doc_id")
    if doc_id is None or not str(doc_id).strip():
        raise HTTPException(400, "doc_id required")
    argv = [
        PY, str(NEWSLINE_REPORTS),
        "report-autofill",
        "--doc-id", str(doc_id).strip(),
    ]
    manual = body.get("manual_links")
    if isinstance(manual, dict):
        for k, v in manual.items():
            if str(k).strip() and str(v).strip():
                argv += ["--manual-link", f"{str(k).strip()}={str(v).strip()}"]
    return argv


@router.post("/api/newsroom/newsline-reports/autofill-preview")
async def api_newsline_reports_autofill_preview(body: dict = Body(...)):
    """Auto-fill preview: scan report Doc for weekday show slots and search links."""
    return await _script(_newsline_report_autofill_argv(body), timeout=120)


@router.post("/api/newsroom/newsline-reports/autofill-apply")
async def api_newsline_reports_autofill_apply(body: dict = Body(...)):
    """Auto-fill apply: search missing links and batchUpdate Google Doc text styles."""
    return await _script(_newsline_report_autofill_argv(body) + ["--apply"], timeout=180)


@router.get("/api/newsroom/newsline-reports/list-report-docs")
async def api_newsline_reports_list_report_docs():
    """List fillable monthly report Google Docs from Google Drive for the doc-picker."""
    return await _script([PY, str(NEWSLINE_REPORTS), "report-list"])


# ------------------------------------------------- recording docs (Sub-tab 4)
RECORDING_DOCS = Path(__file__).parent / "recording_docs.py"


def _recording_docs_argv(body: dict, verb: str) -> list[str]:
    script = str(body.get("script_doc") or "").strip()
    if not script:
        raise HTTPException(400, "script_doc required")
    argv = [PY, str(RECORDING_DOCS), verb, "--script-doc", script]
    for key, flag in (("rundown_doc", "--rundown-doc"), ("prompter_doc", "--prompter-doc")):
        v = str(body.get(key) or "").strip()
        if v:
            argv += [flag, v]
    return argv


@router.post("/api/newsroom/newsline-reports/recording-docs/preview")
async def api_recording_docs_preview(body: dict = Body(...)):
    """Recording docs preview (read-only): extract EVE/NL rundowns + prompter rows."""
    return await _script(_recording_docs_argv(body, "preview"), timeout=120)


@router.post("/api/newsroom/newsline-reports/recording-docs/apply")
async def api_recording_docs_apply(body: dict = Body(...)):
    """Recording docs apply: rename RUNDOWN doc + rewrite both docs (Anchor block kept)."""
    return await _script(_recording_docs_argv(body, "apply"), timeout=300)



# ---------------------------------------------------------------- newsline rundown (Sub-tab 1: Daily NL Rundown)
# Extracts daily NEWSLINE headlines from 'NL & NWB DDMMYY' Google Doc (NL RUNDOWN tab)
# and writes/replaces that day's block into monthly compilation doc 'รันดาวน์ MM/YYYY'.


def _newsline_rundown_argv(body: dict) -> list[str]:
    doc_id = body.get("doc_id")
    if doc_id is None or not str(doc_id).strip():
        raise HTTPException(400, "doc_id required")
    argv = [
        PY, str(NEWSLINE_REPORTS),
        "rundown",
        "--doc-id", str(doc_id).strip(),
    ]
    monthly_id = body.get("monthly_doc_id")
    if monthly_id and str(monthly_id).strip():
        argv += ["--monthly-doc-id", str(monthly_id).strip()]
    return argv


@router.get("/api/newsroom/newsline-rundown/daily-docs")
async def api_newsline_rundown_daily_docs():
    """List recent daily NL & NWB docs from Google Drive for the doc-picker."""
    return await _script([PY, str(NEWSLINE_REPORTS), "daily-docs"])


@router.post("/api/newsroom/newsline-rundown/preview")
async def api_newsline_rundown_preview(body: dict = Body(...)):
    """Extract day's NL rundown and preview target monthly doc update."""
    return await _script(_newsline_rundown_argv(body) + ["preview"])


@router.post("/api/newsroom/newsline-rundown/fill")
async def api_newsline_rundown_fill(body: dict = Body(...)):
    """Extract day's NL rundown and insert/replace in target monthly doc. Idempotent."""
    cmd = ["preview"] if body.get("dry_run") else ["fill"]
    return await _script(_newsline_rundown_argv(body) + cmd, timeout=120)


def _newsline_rundown_fill_month_argv(body: dict) -> list[str]:
    yyyymm = body.get("yyyymm") or body.get("month_str")
    fy_be = body.get("fy_be")
    month = body.get("month")
    monthly_id = body.get("monthly_doc_id")

    if not yyyymm and not (fy_be and month) and not month:
        raise HTTPException(400, "yyyymm (e.g. 202608) or fy_be+month required")

    cmd = "preview-month" if body.get("dry_run") else "fill-month"
    argv = [
        PY, str(NEWSLINE_REPORTS),
        "rundown",
        cmd,
    ]
    if yyyymm:
        argv += ["--month", str(yyyymm).strip()]
    if fy_be:
        argv += ["--fy-be", str(fy_be).strip()]
    if month and not yyyymm:
        argv += ["--month", str(month).strip()]
    if monthly_id and str(monthly_id).strip():
        argv += ["--monthly-doc-id", str(monthly_id).strip()]
    if body.get("dry_run"):
        argv.append("--dry-run")
    return argv


@router.post("/api/newsroom/newsline-rundown/preview-month")
async def api_newsline_rundown_preview_month(body: dict = Body(...)):
    """Preview whole-month NL rundown daily doc matches and target monthly doc update."""
    body_copy = dict(body)
    body_copy["dry_run"] = True
    return await _script(_newsline_rundown_fill_month_argv(body_copy), timeout=120)


@router.post("/api/newsroom/newsline-rundown/fill-month")
async def api_newsline_rundown_fill_month(body: dict = Body(...)):
    """Extract and write whole month of daily NL rundowns into target monthly doc. Idempotent per day."""
    return await _script(_newsline_rundown_fill_month_argv(body), timeout=300)


# ---------------------------------------------------------------- newsline docgen (Sub-tab 3: Bulk Template Scaffold)
# Duplicates 3 templates x 12 months = 36 docs for the fiscal year into target FY folder.


def _newsline_docgen_argv(body: dict) -> list[str]:
    fy_be = body.get("fy_be")
    if fy_be is None or not str(fy_be).strip():
        raise HTTPException(400, "fy_be required")
    argv = [
        PY, str(NEWSLINE_REPORTS),
        "docgen",
        "--fy-be", str(fy_be).strip(),
    ]
    period = body.get("period")
    if period is not None and str(period).strip():
        argv += ["--period", str(period).strip()]
    return argv


@router.post("/api/newsroom/newsline-docgen/preview")
async def api_newsline_docgen_preview(body: dict = Body(...)):
    """Preview 36 planned docs for the fiscal year."""
    return await _script(_newsline_docgen_argv(body) + ["preview"])


@router.post("/api/newsroom/newsline-docgen/generate")
async def api_newsline_docgen_generate(body: dict = Body(...)):
    """Duplicate 3 templates x 12 months for the fiscal year into target FY folder. Idempotent."""
    cmd = ["preview"] if body.get("dry_run") else ["generate"]
    return await _script(_newsline_docgen_argv(body) + cmd, timeout=300)


# ---------------------------------------------------------------- rewrite
# Source article → two-layer broadcast script via the news-producer Rules Gem.
# Rides app/zai.py (the OmniRoute gateway, NOT z.ai direct), so the pass keeps
# working past a z.ai quota wall. Editorial hard rule (2026-08-27): every Thai
# name in the body rides inside the overlay — **English Name [ชื่อไทย]** — and
# app.name_check enforces it post-hoc (search-before-name happens in the IDE
# lane; the metered lane confirms from knowledge or romanizes RTGS-style).


_THAI_RUN_RE = re.compile(r'[฀-๿]{3,}')


def _strip_fabricated_thai(body: str, source: str) -> str:
    """Remove Thai text not present verbatim in the source.

    The overlay rule forbids transliterating/guessing Thai names, but the LLM
    fabricates Thai renderings anyway -- in ever-shifting formats (**[Eng(Thai)]**,
    [Eng](Thai), inline Thai, ...). Format-independent guard: every Thai run >=3 chars
    not found verbatim in the source is fabricated -> stripped, along with its wrapper.
    Source-faithful Thai (titles, real names) is kept.
    """
    for m in reversed(list(_THAI_RUN_RE.finditer(body))):
        if m.group(0) in source:
            continue
        s, e = m.start(), m.end()
        # Consume parentheses wrapping the fabricated Thai: "English(Thai)"
        if s > 0 and body[s - 1] == '(' and e < len(body) and body[e] == ')':
            s -= 1
            e += 1
        body = body[:s] + body[e:]
    # Tidy: empty parens from multi-word Thai stripped separately, then orphaned
    # brackets — but SPARE bracket groups that contain Thai: the 2026-08-27
    # overlay format is **English Name [ชื่อไทย]** and the brackets are load-
    # bearing (app.name_check reads them as the only legal Thai zone).
    body = re.sub(r'\(\s*\)', '', body)
    body = re.sub(
        r'\[([^\[\]()]+)\]',
        lambda m: m.group(0) if re.search(r'[\u0e00-\u0e7f]', m.group(1)) else m.group(1),
        body,
    )
    return body


def _gem_text() -> str:
    """Load Ben's voice gem body (## Role & Purpose -> ### Output)."""
    if not BEN_GEM.exists():
        raise HTTPException(500, f"Ben gem not found at {BEN_GEM}")
    md = BEN_GEM.read_text(encoding="utf-8")
    marker = "## Role & Purpose"
    i = md.find(marker)
    if i < 0:
        raise HTTPException(500, "Ben gem missing '## Role & Purpose'")
    body = md[i:]
    j = body.find("### Output")
    if j >= 0:
        body = body[:j]
    return body.strip()


def _seo_gem_text() -> str:
    """Load Thailand NOW SEO gem body (Role & Purpose + House Style + Section 4 AI SEO Block rules)."""
    if not SEO_GEM.exists():
        raise HTTPException(500, f"SEO gem not found at {SEO_GEM}")
    md = SEO_GEM.read_text(encoding="utf-8")
    role_i = md.find("## Role & Purpose")
    out_req_i = md.find("## Output Requirements")
    block_i = md.find("### 4. AI SEO Block (2 versions)")
    if role_i >= 0 and out_req_i >= 0 and block_i >= 0:
        head = md[role_i:out_req_i]
        end_sep = md.find("\n---\n\n## Thailand NOW Content", block_i)
        block = md[block_i:end_sep] if end_sep >= 0 else md[block_i:]
        return (head.strip() + "\n\n" + block.strip()).strip()
    return md.strip()


def _parse_ben_json(raw: str) -> tuple[str, str, str]:
    """Parse the model's ``{title, title_th, body}`` reply (the gem's ### Output
    schema). Tolerates a stray code fence or preamble by slicing the outermost
    braces. Falls back to ``("", "", <raw>)`` so a parse miss still surfaces the
    copy as the body rather than dropping it."""
    i, j = raw.find("{"), raw.rfind("}")
    if i >= 0 and j > i:
        try:
            d = json.loads(raw[i:j + 1])
            if isinstance(d, dict):
                return (
                    str(d.get("title", "")).strip(),
                    str(d.get("title_th", "")).strip(),
                    str(d.get("body", "")).strip(),
                )
        except (ValueError, TypeError):
            pass
    return "", "", raw.strip()


@router.post("/api/newsroom/rewrite")
async def api_rewrite(body: dict = Body(...)):
    """Run the Script-box text through Ben's gem (broadcast prose + **name** markers)
    and the Thailand NOW SEO gem (AI SEO Block Version A+B).

    Returns ``{"rewritten": ..., "seo": ...}``."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "nothing to rewrite — the Script box is empty")
    ben_prompt = (
        _gem_text()
        + "\n\n=== OUTPUT OVERRIDE (this replaces the ### Output schema) ===\n"
        "Return ONLY a single JSON object — no code fence, no preamble, no commentary — with "
        "EXACTLY these keys:\n"
        '{"title": "<a short, SEO-friendly English title>", '
        '"title_th": "<the original Thai title from the source, retained exactly; translate from '
        'the English title if the source has none>", '
        '"body": "<the broadcast rewrite>"}\n'
        "Rules for the `body` string (Ben's hard rules and voice still apply):\n"
        "- Readable broadcast prose in 2-4 separate paragraphs; separate paragraphs with a blank "
        "line, written as \\n\\n inside the JSON string. Never one unbroken block.\n"
        "- Wrap every person's NAME in **double-stars** per the NAME OVERLAY rule below.\n"
        "- Wrap every date, time, and relative-time expression in ~~…~~ markers "
        "(e.g. ~~July 15, 2026~~, ~~3:00 PM~~, ~~next month~~). These become underlined in the Doc.\n"
        "- Write every number as numerals with commas (60,000, 69, 15 years) — never spelled out "
        "('sixty thousand', 'sixty-nine'); keep the words million/billion/trillion only after a "
        "numeral ('3.2 million').\n"
        "- **double-stars** and ~~tildes~~ are the ONLY markup allowed in `body`; no other markdown.\n\n"
        "=== NAME OVERLAY RULE (persons AND places) ===\n"
        "For every PERSON or PLACE the source names — persons, and places such as "
        "sub-district, district, province, or river — render the FIRST mention as:\n"
        "**English Name [ชื่อไทยเดิม]**\n"
        "- The bracket holds the name EXACTLY as the source writes it (Thai only, no "
        "title). Square brackets; bold wraps the whole form. Later mentions: English only.\n"
        "- If the SOURCE gives the name in ENGLISH: use that English name exactly as "
        "written, bolded (**EnglishName**). Do NOT generate Thai script for an "
        "English-source name.\n"
        "- If the SOURCE gives the name in THAI: confirm the official or established "
        "English rendering from your knowledge and use it; when you cannot confirm one, "
        "romanize conservatively (places: Royal Thai General System; persons: common "
        "press style) and STILL bracket the source Thai. NEVER leave a Thai name bare "
        "in the body — every Thai name rides inside the bracket.\n"
        "- Names NEVER carry titles, ranks, or honorifics (no นาย/นาง/ตำแหน่ง) — the "
        "sentence carries the role, the overlay carries only the name.\n"
        "NEVER invent a Thai rendering for a name the source does not contain in Thai. "
        "NARROW CARVE-OUT: knowledge is allowed ONLY to supply a named person's or "
        "place's English name-form. Never use knowledge to ADD names, dates, figures, "
        "events, or any other facts — all other content is SOURCE-ONLY.\n\n"
        "=== CRITICAL EDITORIAL RULE (overrides everything above) ===\n"
        "Use ONLY the information in the SOURCE ARTICLE below. Never add "
        "dates, ranks, titles, agencies, figures, locations, or any fact from your "
        "own knowledge or training. Specifically:\n"
        "- Copy each person's rank/title EXACTLY as the source gives it — never "
        "promote, demote, or infer one (source says Prime Minister → not Deputy).\n"
        "- Do NOT invent a day of week, absolute date, or which agency acts unless "
        "the source states it. No date in source → write none.\n"
        "- Do NOT guess transliterations. Established English name-forms only (per NAME "
        "OVERLAY above), else conservative romanization with the source Thai bracketed.\n\n"
        "=== SOURCE ARTICLE ===\n" + text
    )
    seo_system = (
        _seo_gem_text()
        + "\n\n=== CRITICAL OUTPUT OVERRIDE ===\n"
        "Produce ONLY the AI SEO Block — Version A (40-60w summary). "
        "Do NOT produce Version B (Key Points), focus keyphrases, meta descriptions, or hashtags.\n"
        "Output ONLY the Version A summary paragraph."
    )
    out_ben, out_seo = await asyncio.gather(
        zai.zai_message(ben_prompt, max_tokens=9000, timeout=120),
        zai.zai_message(text, system=seo_system, max_tokens=4000, timeout=60),
    )
    if not out_ben.strip():
        raise HTTPException(502, "rewrite came back empty")
    if not out_seo.strip():
        raise HTTPException(502, "seo generation came back empty")
    title, title_th, body = _parse_ben_json(out_ben)
    # ponytail: code-level guard -- LLM fabricates Thai-name overlays despite the rule.
    # Scoped to the BODY (the on-air read); title_th is an editor/translation field the
    # guard must not clobber (office guards the whole blob, but office returns it raw --
    # Railjack surfaces title_th as "TH:", so protect it). Strips Thai not in the source.
    body = _strip_fabricated_thai(body, text)
    # Reassemble the CANONICAL blob (bracket overlays intact) — checks read
    # this form; the SERVED form renders overlays to aired parens (below).
    canonical = f"EN: {title}\nTH: {title_th}\n\n{body}" if (title or title_th) else body
    namecheck = _safe_namecheck(canonical)
    stylecheck = _safe_stylecheck(canonical)
    return {
        "rewritten": _render_overlays(canonical),
        "seo": out_seo,
        "namecheck": namecheck,
        "stylecheck": stylecheck,
    }


# ------------------------------------------------------- infographics

# Advisory director pass: marks which BODY paragraphs deserve a motion graphic and
# emits a paste-ready prompt for Naz's Google Flow "Broadcast Infographic Pro" app.
# That app owns ALL styling/art/color, and its Information Intake section has
# exactly TWO inputs — so emitting art direction here would be noise the app ignores.
_INFO_SYSTEM = """You are a broadcast infographics director for Thailand NOW.

You receive a TV news script as NUMBERED paragraphs. Decide which paragraphs should be
backed by a motion infographic. You do NOT rewrite, summarize, reorder, or reproduce the
script — you only return your picks as JSON. The script itself is reassembled by code.

RULES:
1. NEVER pick paragraph 1 (the intro/lede). Valid picks start at paragraph 2.
2. SOURCE-ONLY: `facts` may contain ONLY figures already present in that paragraph.
   Never invent, round, extrapolate, or add a statistic — copy them verbatim.
3. BE SELECTIVE. Pick only paragraphs with genuinely visual content: numbers, money,
   percentages, counts, comparisons, rankings, timelines, before/after, routes, or
   process steps. Skip pure quotes and soft narrative. An EMPTY list is a valid,
   correct answer when nothing qualifies.

Return ONLY a JSON object — no code fence, no commentary:
{"picks": [{"paragraph": <int>, "headline": "<5-8 words>", "why": "<one line: the data worth showing>",
            "intake": "<editorial beat + short framing clause>",
            "facts": "<the exact figures from that paragraph, verbatim, comma-separated>"}]}

`intake` and `facts` are the ONLY two fields the target app accepts. Never emit
Aesthetic, Art Style, Background, Color, Tone, or Dimension — the app sets those itself."""


def _annotate_infographics(text: str, picks: list) -> str:
    """Reassemble the script with INFOGRAPHIC blocks above the picked paragraphs.

    The LLM only chooses paragraphs; the prose is copied verbatim here. Earlier
    versions asked the model to reproduce the script and it twice corrupted the
    news — once prepending a block above the lede, once DELETING the lede outright.
    Rebuilding in code makes 'never edits the news' true by construction, so
    paragraph 1 can't be annotated and no wording can drift.
    """
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    by_para: dict[int, list] = {}
    for p in picks:
        try:
            i = int(p.get("paragraph"))
        except (TypeError, ValueError):
            continue
        # 1-based from the model; paragraph 1 (lede) is never annotatable.
        if 2 <= i <= len(paras):
            by_para.setdefault(i, []).append(p)

    out: list[str] = []
    for n, para in enumerate(paras, start=1):
        for p in by_para.get(n, []):
            out.append(
                "----- INFOGRAPHIC: %s -----\n"
                "Why: %s\n"
                "Broadcast Infographic Pro:\n"
                "  Information Intake:      %s\n"
                "  News fact + data point:  %s\n"
                "-----------------------------------------------------------"
                % (p.get("headline", ""), p.get("why", ""), p.get("intake", ""), p.get("facts", ""))
            )
        out.append(para)
    return "\n\n".join(out)


@router.post("/api/newsroom/infographic/suggest")
async def api_infographic_suggest(body: dict = Body(...)):
    """Annotate the Script-box text with Broadcast Infographic Pro prompts.

    Advisory only — returns ``{"annotated": ..., "count": n}``; touches no Google
    Doc, and the news prose is copied verbatim (see ``_annotate_infographics``)."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "nothing to analyse — the Script box is empty")
    numbered = "\n\n".join(
        "[%d] %s" % (i, p)
        for i, p in enumerate(
            [p for p in re.split(r"\n\s*\n", text) if p.strip()], start=1)
    )
    out = await zai.zai_message(numbered, system=_INFO_SYSTEM, max_tokens=4000, timeout=120)
    if not out.strip():
        raise HTTPException(502, "infographic pass came back empty")
    raw = out.strip()
    if raw.startswith("```"):  # strip a stray code fence
        raw = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw).strip()
    try:
        picks = json.loads(raw).get("picks") or []
    except Exception:
        raise HTTPException(502, "infographic pass returned unparseable JSON")
    return {"annotated": _annotate_infographics(text, picks), "count": len(picks)}


# Antigravity IDE rewrite writes its handoff here (see 10-knowledge/newsroom-rewrite-
# antigravity-handoff.md). Module-level so tests monkeypatch it off real /tmp.
_REWRITE_HANDOFF = Path("/tmp/newsroom-rewrite/latest.json")


def _safe_namecheck(text: str) -> dict:
    """Thai-name fact-check that can never kill a good rewrite (advisory hook).
    [inf] markers are stripped first — they are presentation, not checkable
    script (parity with Somatic: markers never reach the checker)."""
    try:
        return check_rewritten(_strip_inf(text))
    except Exception as exc:  # pragma: no cover — checker is pure + tested
        return {
            "errors": [],
            "warnings": [{"kind": "checker", "name": "", "detail": str(exc)}],
            "names": {"verified": [], "unverified": []},
            "ok": True,
        }


def _safe_stylecheck(text: str) -> dict:
    """Ben-voice / anti-slop check that can never kill a good rewrite (advisory)."""
    try:
        return check_style(text)
    except Exception as exc:  # pragma: no cover — checker is pure + tested
        return {
            "errors": [],
            "warnings": [{"kind": "checker", "name": "", "detail": str(exc)}],
            "stats": {},
            "ok": True,
        }


# Overlay render shapes, most specific first:
#   legacy  **[English(Thai)]** / [English(Thai)]  →  **English (Thai)**
#   current **English [Thai]**   / English [Thai]  →  English (Thai)
# Thai-bearing only — non-Thai brackets are prose, not overlays.
_OVERLAY_LEGACY_RE = re.compile(
    r"\*{0,2}\[([^\[\]\n()]+)\(([^()\[\]\n]*[\u0e00-\u0e7f][^()\[\]\n]*)\)\]\*{0,2}"
)
_OVERLAY_BRACKET_RE = re.compile(r"\s*\[([^\[\]\n]*[\u0e00-\u0e7f][^\[\]\n]*)\]")


def _render_overlays(text: str) -> str:
    """Render Thai-name overlays to aired form: **English (ชื่อไทย)**.

    Square brackets are the PIPELINE format — the machine-readable zone that
    app.name_check treats as the only legal Thai and the strip guard spares.
    They were never meant for the anchor's eyes: every script this hub serves
    (rewrite, CONVERT → Script box → preview → Docs → radio) renders them as
    parentheses. Checks always run on the canonical bracket form BEFORE this;
    the transform is idempotent, so CONVERT-again can't double-apply.
    """
    text = _OVERLAY_LEGACY_RE.sub(
        lambda m: f"**{m.group(1).strip()} ({m.group(2).strip()})**", text
    )
    text = _OVERLAY_BRACKET_RE.sub(lambda m: f" ({m.group(1).strip()})", text)
    return text


@router.post("/api/newsroom/rewrite/convert")
async def rewrite_convert() -> dict:
    """FREE IDE CONVERT — relay the Antigravity rewrite handoff (``_REWRITE_HANDOFF`` JSON) as
    ``{rewritten, seo}``, the SAME shape ``api_rewrite`` returns so the frontend path is identical.
    No LLM, no body. Soft-fails to HTTP 200 ``{rewritten:"", seo:"", errors:[...]}`` when the
    handoff is missing/unparseable — points the user at 📋 IDE REWRITE first.
    Relays ``namecheck`` (Thai-name fact-check) and ``stylecheck`` (Ben-voice/anti-slop)
    too — advisory, never blocking."""
    miss = {"rewritten": "", "seo": "", "errors": ["no IDE handoff file — run 📋 IDE REWRITE first"]}
    if not _REWRITE_HANDOFF.exists():
        return miss
    try:
        data = json.loads(_REWRITE_HANDOFF.read_text(encoding="utf-8"))
    except Exception:
        return miss
    rewritten = (data.get("rewritten") or "").strip()
    namecheck = _safe_namecheck(rewritten)
    stylecheck = _safe_stylecheck(rewritten)
    return {
        "rewritten": _render_overlays(rewritten),
        "seo": (data.get("seo") or "").strip(),
        "errors": [],
        "namecheck": namecheck,
        "stylecheck": stylecheck,
    }


# ---------------------------------------------------------------- health


@router.get("/api/newsroom/probe")
async def api_probe() -> dict:
    """Newstank reachability (drives the panel's health pip). Runs a real
    `queue.py list` — cheap enough on demand, never polled."""
    try:
        rc, _, _ = await _run([PY, str(QUEUE), "list", "--json", "--author", "all"], timeout=30)
        return {"ok": rc == 0}
    except HTTPException:
        return {"ok": False}


# ── ＋wiki name register (Naz 2026-09-01) ──────────────────────────────
# Closes the name-check loop at the point of pain: the checker flags
# unverified names, the IDE lane verifies the English form but is read-only
# on the vault — so verified names never landed in the registry. The panel's
# ＋wiki button posts the verified pair here; the note lands in the vault
# name-wiki and the next check reads clean.

_NAME_WIKI = Path.home() / "Cephalon" / "10-knowledge" / "name-wiki"


def _name_slug(english: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", english.lower()).strip("-")


@router.post("/api/newsroom/namecheck/register")
async def namecheck_register(body: dict = Body(...)) -> dict:
    """Register a verified ``{english, thai, kind, source}`` pair in the vault
    name-wiki (one template note, atomic tmp+mv). Refusals — duplicate Thai
    key, title-carrying name, slug collision — return ``ok:false`` with a
    reason; only malformed input raises 400. Advisory tool, never blocking."""
    english = (body.get("english") or "").strip()
    thai = (body.get("thai") or "").strip()
    kind = (body.get("kind") or "person").strip()
    source = (body.get("source") or "").strip()
    if not english or not re.search(r"[A-Za-z]", english):
        raise HTTPException(400, "english name missing")
    if len(english) > 120 or len(thai) > 120 or len(source) > 500:
        raise HTTPException(400, "field too long")
    if re.search(r"[\u0e00-\u0e7f]", english):
        raise HTTPException(400, "english name must be Latin")
    if not re.search(r"[\u0e00-\u0e7f]", thai):
        raise HTTPException(400, "thai name missing — the bracket Thai is the registry key")
    if kind not in ("person", "place"):
        raise HTTPException(400, "kind must be person or place")
    if thai.startswith(_HONORIFICS):
        return {
            "ok": False,
            "reason": "thai name carries a title/rank — strip to the bare name",
        }
    reg, _ = load_registry(_NAME_WIKI)
    if thai in reg:
        return {
            "ok": False,
            "reason": f'already registered as "{reg[thai] or thai}"',
        }
    slug = _name_slug(english)
    if not slug:
        raise HTTPException(400, "english name must contain letters")
    note = _NAME_WIKI / f"{slug}.md"
    if note.exists():
        return {"ok": False, "reason": f"note file {slug}.md already exists"}
    today = datetime.now().strftime("%Y-%m-%d")
    tags = "name-wiki, person" if kind == "person" else "name-wiki, place"
    text = (
        "---\n"
        f'title: "{english}"\n'
        f"date: {today}\n"
        f"updated: {today}\n"
        f"tags: [{tags}]\n"
        "category: knowledge\n"
        "status: active\n"
        f'english: "{english}"\n'
        f'thai: "{thai}"\n'
        f"kind: {kind}\n"
        f"first-seen: {today}\n"
        f'source: "{source}"\n'
        "---\n\n"
        f"# {english} ({thai})\n\n"
        f"Registered {today} via the NEWSROOM panel name-check (＋wiki button).\n"
    )
    _NAME_WIKI.mkdir(parents=True, exist_ok=True)
    tmp = note.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.rename(note)
    return {"ok": True, "file": note.name, "english": english, "thai": thai}


# ─────────────────────────────────────────────────────────────────────
# INFOGRAPHICS — [inf]…[inf/] TV-safe PNG pipeline + Flow loop prompts.
# Parity port of Somatic fea25ee..58314bd (2026-09-02, handoff via Naz).
# Catalogs (_INF_STYLES/_INF_PALETTES/_INF_MOTIONS), art lines and the
# _LOOP_CROSSFADE_CMD de-jerk cure are VERBATIM by invariant (panel must
# mirror the motion catalog; the cure is lab-verified — do not "upgrade").
# Wiring is Railjack-native: router routes, zai helper, _run/_script stdin.
# ─────────────────────────────────────────────────────────────────────


_INF_MARKER_RE = re.compile(r"(\[inf/?\])")


def parse_inf_blocks(text: str) -> tuple[list[str], list[str]]:
    """Parse [inf]...[inf/] blocks from text.

    Markers work on their own lines (canonical) AND inline within a line.
    Returns (blocks, warnings). An unclosed block runs to end of text with a
    warning; a bare `[inf]` used as a closer auto-closes with a warning —
    content is never lost either way.
    """
    blocks: list[str] = []
    warnings: list[str] = []
    if not text:
        return blocks, warnings
    in_block = False
    current: list[str] = []

    def _flush() -> None:
        content = "\n".join(current).strip()
        if content:
            blocks.append(content)
        else:
            warnings.append("empty [inf] block")
        current.clear()

    for line in text.splitlines():
        for token in _INF_MARKER_RE.split(line):
            tok = token.strip()
            if tok == "[inf]":
                if in_block:
                    warnings.append("unclosed [inf] block — auto-closing before new block")
                    _flush()
                in_block = True
            elif tok == "[inf/]":
                if in_block:
                    _flush()
                    in_block = False
                # stray closer outside a block: ignore (lenient)
            elif in_block and token.strip():
                current.append(token.strip())

    if in_block:
        warnings.append("unclosed [inf] block — ran to end of text")
        _flush()

    return blocks, warnings


def _strip_inf(text: str) -> str:
    """Remove [inf]/[inf/] markers (own-line OR inline), keeping inner text.
    Idempotent."""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip() in ("[inf]", "[inf/]"):
            continue
        if "[inf" in line:
            stripped = _INF_MARKER_RE.sub("", line)
            stripped = re.sub(r"\s{2,}", " ", stripped).strip()
            lines.append(stripped)
        else:
            lines.append(line)
    return "\n".join(lines)


# Art-direction styles. `preset` MUST be one of the CLI's infographic --style
# values (notebooklm 0.8.1 `generate infographic --help`: auto|sketch-note|
# professional|bento-grid|editorial|instructional|bricks|clay|anime|kawaii|
# scientific). `moods` gate the auto-pick; `general` = safe for neutral scripts.
_INF_STYLES: list[dict] = [
    # ── newsroom workhorses (neutral-safe) ──
    {
        "id": "flat-navy",
        "label": "Flat Navy",
        "preset": "professional",
        "art": "flat vector motion-graphics; deep vertical gradient background over a faint thin-line geometric grid, clean sans-serif type, one bold accent.",
        "moods": ["hard-news", "business", "science", "general"],
        "bg_tone": "dark",
    },
    {
        "id": "editorial-print",
        "label": "Editorial Print",
        "preset": "editorial",
        "art": "editorial print-news look; warm paper background with a subtle tone-on-tone gradient and faint halftone dot texture, ink-drawn rules, serif display type.",
        "moods": ["hard-news", "business", "culture", "general"],
        "bg_tone": "light",
    },
    {
        "id": "swiss",
        "label": "Swiss Grid",
        "preset": "bento-grid",
        "art": "strict modernist grid; near-white background with a barely-there cool gradient and thin geometric section rules, heavy headline type, one bold accent.",
        "moods": ["hard-news", "business", "sport", "general"],
        "bg_tone": "light",
    },
    {
        "id": "glass-panel",
        "label": "Glass Panel",
        "preset": "bento-grid",
        "art": "frosted translucent panels floating over a deep gradient background with a faint geometric mesh; panels static, no shimmer.",
        "moods": ["business", "tech", "science", "general"],
        "bg_tone": "dark",
    },
    # ── science / tech / environment ──
    {
        "id": "isometric",
        "label": "Isometric",
        "preset": "scientific",
        "art": "3D isometric flat-vector diorama on a deep gradient background with faint concentric geometric rings.",
        "moods": ["science", "business", "tech", "environment"],
        "bg_tone": "dark",
    },
    {
        "id": "blueprint",
        "label": "Blueprint",
        "preset": "instructional",
        "art": "architectural blueprint; deep background with a soft radial gradient under a precise thin grid, technical line drawings, dashed measure lines.",
        "moods": ["science", "tech", "environment", "business"],
        "bg_tone": "dark",
    },
    {
        "id": "chalkboard",
        "label": "Chalkboard",
        "preset": "sketch-note",
        "art": "classroom chalkboard; deep green-black gradient with faint chalk-dust texture, hand-drawn chalk strokes and arrows.",
        "moods": ["science", "soft", "culture"],
        "bg_tone": "dark",
    },
    {
        "id": "neon-data",
        "label": "Neon Data",
        "preset": "bento-grid",
        "art": "broadcast data-terminal; near-black gradient with a thin geometric grid and static glowing panel edges, no shimmer.",
        "moods": ["tech", "business", "science"],
        "bg_tone": "dark",
    },
    {
        "id": "botanical",
        "label": "Botanical Craft",
        "preset": "clay",
        "art": "botanical paper-craft; warm sand background with a soft daylight gradient and faint abstract leaf silhouettes, layered paper leaves.",
        "moods": ["environment", "soft", "culture"],
        "bg_tone": "light",
    },
    # ── hard-news urgency ──
    {
        "id": "line-art",
        "label": "Line Art",
        "preset": "sketch-note",
        "art": "minimal line art; near-black gradient with a faint geometric dot lattice, thin elegant line work, one bright accent.",
        "moods": ["hard-news", "science", "culture"],
        "bg_tone": "dark",
    },
    {
        "id": "mono-alert",
        "label": "Mono Alert",
        "preset": "professional",
        "art": "wire-agency urgency; near-white diagonal gradient with hairline rules, heavy black headline type, one loud accent, zero decoration beyond the gradient.",
        "moods": ["hard-news"],
        "bg_tone": "light",
    },
    # ── culture / soft / celebration / sport ──
    {
        "id": "papercut",
        "label": "Papercut",
        "preset": "clay",
        "art": "layered paper-cut craft diorama; cream background with a soft warm gradient and subtle paper-grain texture.",
        "moods": ["culture", "soft", "celebration"],
        "bg_tone": "light",
    },
    {
        "id": "retro-travel",
        "label": "Retro Travel",
        "preset": "editorial",
        "art": "1950s travel poster; sun-faded gradient background with vintage print texture and bold retro serif display type.",
        "moods": ["culture", "soft", "celebration"],
        "bg_tone": "light",
    },
    {
        "id": "brick-lab",
        "label": "Brick Lab",
        "preset": "bricks",
        "art": "toy building-brick diorama; light grey gradient with faint oversized brick-outline shapes, chunky brick builds, playful but tidy.",
        "moods": ["soft", "celebration", "sport"],
        "bg_tone": "light",
    },
    {
        "id": "anime-pop",
        "label": "Anime Pop",
        "preset": "anime",
        "art": "clean anime key-visual; cream background with a soft sunset gradient blush and static speed-line accents, bold linework.",
        "moods": ["sport", "celebration", "culture"],
        "bg_tone": "light",
    },
    {
        "id": "kawaii",
        "label": "Kawaii",
        "preset": "kawaii",
        "art": "soft pastel clay diorama; pastel gradient with a faint cloud-dot texture and rounded soft shapes.",
        "moods": ["soft", "celebration"],
        "bg_tone": "light",
    },
]
_INF_STYLES_BY_ID = {s["id"]: s for s in _INF_STYLES}

# Color-scheme layer — ROTATES INDEPENDENTLY of style. `tone` must match the
# style's bg_tone (contrast safety: dark styles get light type, light styles
# get dark type); `moods` gate the draw the same way style moods do.
# bg = gradient pair; accent = the single loud color; type = text color.
_INF_PALETTES: list[dict] = [
    # ── dark-tone (light type) ──
    {"id": "abyss-navy", "name": "Abyss Navy", "tone": "dark",
     "bg": ["#0B1F3A", "#16345C"], "accent": "#56B4E9", "type": "#F2F6FA",
     "moods": ["hard-news", "business", "science", "general"]},
    {"id": "charcoal-mint", "name": "Charcoal Mint", "tone": "dark",
     "bg": ["#1A1D21", "#0D1117"], "accent": "#3FB950", "type": "#EEF3EE",
     "moods": ["tech", "business", "science", "general"]},
    {"id": "midnight-magenta", "name": "Midnight Magenta", "tone": "dark",
     "bg": ["#101820", "#1B1026"], "accent": "#DB61A2", "type": "#F5EEF5",
     "moods": ["tech", "celebration", "culture"]},
    {"id": "ink-crimson", "name": "Ink Crimson", "tone": "dark",
     "bg": ["#141414", "#26100E"], "accent": "#E53935", "type": "#F5F0EE",
     "moods": ["hard-news"]},
    {"id": "deep-teal-amber", "name": "Deep Teal Amber", "tone": "dark",
     "bg": ["#06323E", "#0A4A56"], "accent": "#E69F00", "type": "#EFF7F5",
     "moods": ["science", "environment", "business"]},
    {"id": "storm-blue", "name": "Storm Blue", "tone": "dark",
     "bg": ["#1B2A3A", "#2E4A62"], "accent": "#A8C6E8", "type": "#EDF3F9",
     "moods": ["hard-news", "science", "business"]},
    {"id": "aubergine-gold", "name": "Aubergine Gold", "tone": "dark",
     "bg": ["#241532", "#3A2150"], "accent": "#F0C24B", "type": "#F7F2EA",
     "moods": ["culture", "celebration"]},
    {"id": "forest-night", "name": "Forest Night", "tone": "dark",
     "bg": ["#0E241A", "#16382A"], "accent": "#66BB6A", "type": "#EDF5EE",
     "moods": ["environment", "soft", "science"]},
    {"id": "espresso-rose", "name": "Espresso Rose", "tone": "dark",
     "bg": ["#26191A", "#3B2527"], "accent": "#E08794", "type": "#F7EFF0",
     "moods": ["soft", "culture"]},
    {"id": "slate-signal", "name": "Slate Signal", "tone": "dark",
     "bg": ["#23272E", "#343B45"], "accent": "#FF7043", "type": "#F0F2F5",
     "moods": ["sport", "business", "tech"]},
    # ── light-tone (dark type) ──
    {"id": "paper-ink", "name": "Paper Ink", "tone": "light",
     "bg": ["#F7F1E3", "#EFE5CF"], "accent": "#D55E00", "type": "#33302B",
     "moods": ["culture", "business", "hard-news", "general"]},
    {"id": "clinical-white", "name": "Clinical White", "tone": "light",
     "bg": ["#FFFFFF", "#E9EBED"], "accent": "#C62828", "type": "#16181A",
     "moods": ["hard-news", "science", "business", "general"]},
    {"id": "sand-terracotta", "name": "Sand Terracotta", "tone": "light",
     "bg": ["#F1E9DC", "#E7D8BE"], "accent": "#C96F4A", "type": "#2E2A24",
     "moods": ["environment", "soft", "culture"]},
    {"id": "mist-blue", "name": "Mist Blue", "tone": "light",
     "bg": ["#EEF3F7", "#DCE6EF"], "accent": "#1F4E79", "type": "#1C2732",
     "moods": ["business", "science", "tech", "general"]},
    {"id": "blush-cream", "name": "Blush Cream", "tone": "light",
     "bg": ["#FFF6EC", "#FFE2C6"], "accent": "#FF7043", "type": "#3A2A20",
     "moods": ["soft", "celebration", "sport", "culture"]},
    {"id": "mint-cream", "name": "Mint Cream", "tone": "light",
     "bg": ["#F0F7F1", "#DFEEDF"], "accent": "#2E7D32", "type": "#1F2B20",
     "moods": ["environment", "science", "soft"]},
    {"id": "lavender-air", "name": "Lavender Air", "tone": "light",
     "bg": ["#FDF2F4", "#F2E1EF"], "accent": "#8E4A8E", "type": "#332433",
     "moods": ["soft", "celebration"]},
    {"id": "butter-sky", "name": "Butter Sky", "tone": "light",
     "bg": ["#FFF9E6", "#F5ECC8"], "accent": "#E8A100", "type": "#332B14",
     "moods": ["celebration", "culture", "soft"]},
    {"id": "gallery-grey", "name": "Gallery Grey", "tone": "light",
     "bg": ["#F4F4F4", "#E4E4E6"], "accent": "#D32F2F", "type": "#1A1C1E",
     "moods": ["hard-news", "business", "sport", "general"]},
    {"id": "lagoon", "name": "Lagoon", "tone": "light",
     "bg": ["#E6F4F1", "#CDE8E2"], "accent": "#0E6E62", "type": "#12312C",
     "moods": ["environment", "soft", "celebration", "science"]},
]
_INF_PALETTES_BY_ID = {p["id"]: p for p in _INF_PALETTES}


def _palette_desc(pal: dict) -> str:
    tone_txt = ("light type on dark" if pal["tone"] == "dark"
                else "dark type on light")
    return (f"{pal['name']} — background gradient {pal['bg'][0]} to {pal['bg'][1]}, "
            f"accent {pal['accent']}, type {pal['type']} ({tone_txt}). "
            f"Use ONLY these colors; no others.")


def _pick_palette(moods: list[str], tone: str) -> dict:
    """Random palette matching the style's tone first (contrast safety),
    the article's moods second; falls back to any palette of the tone."""
    pool = [p for p in _INF_PALETTES
            if p["tone"] == tone and any(m in p["moods"] for m in moods)]
    if not pool:
        pool = [p for p in _INF_PALETTES if p["tone"] == tone]
    return dict(random.choice(pool))

# Mood lexicon — one bucket per mood tag, matched against the WHOLE script.
# Word-boundary, case-insensitive; a trailing * = any word suffix (touris*
# hits tourist/tourists/tourism). Kept deliberately phrase-precise: a tag
# firing widens the random style pool, it never narrows TV-safety.
_MOOD_LEXICON: dict[str, tuple[str, ...]] = {
    "hard-news": (
        "war", "conflict", "clash", "attack", "airstrike", "military", "troop*",
        "insurgent", "violence", "bombing", "shooting", "coup", "protest",
        "crash", "accident", "derail*", "explosion", "flood", "earthquake",
        "wildfire", "storm", "typhoon", "landslide", "drought", "evacuat*",
        "casualt*", "killed", "death", "died", "injur*", "court", "police",
        "arrest*", "trial", "sentenc*", "guilty", "fraud", "scam", "smuggl*",
        "traffick*", "corruption", "lawsuit", "crisis", "election", "parliament",
        "senate", "cabinet", "minister*", "policy", "border",
    ),
    "business": (
        "econom*", "gdp", "export*", "import*", "trade", "invest*", "stock",
        "baht", "inflation", "interest rate", "bank", "budget", "deficit",
        "surplus", "revenue", "tariff*", "manufactur*", "factory",
        "unemploym*", "jobless", "merger", "acquisition", "ipo", "subsid*",
        "fiscal", "touris*", "market*", "compan*", "firm*", "ceo", "quarterly",
    ),
    "tech": (
        "ai", "artificial intelligence", "tech", "digital", "innovation",
        "startup*", "cyber", "data center", "robot*", "semiconductor", "5g",
        "internet", "satellite", "app", "apps", "online", "platform*",
    ),
    "science": (
        "research*", "scientif*", "laborator*", "health", "hospital*",
        "disease", "virus", "vaccine", "outbreak", "medical", "patient*",
        "mental", "epidemi*", "space", "climat*", "environment*", "carbon",
        "emission*", "conservation", "wildlife", "national park", "pollution",
        "waste", "energy", "solar", "renewable", "biodiversity", "specie*",
        "forest*",
    ),
    "culture": (
        "culture", "cultural", "festival*", "heritage", "tradition*", "temple*",
        "ceremony", "exhibition", "concert*", "museum*", "unesco", "buddhist",
        "monk*", "historical", "anniversary", "centenn*", "art", "arts",
        "film*", "music", "dance", "theatre", "theater", "literature",
        "religion",
    ),
    "soft": (
        "food", "cuisine", "recipe*", "restaurant*", "street food", "travel*",
        "tourist*", "hotel*", "lifestyle", "fashion", "beauty", "wellness",
        "communit*", "village*", "shopping", "spa", "leisure", "family",
        "pet", "pets",
    ),
    "celebration": (
        "celebrat*", "new year", "songkran", "loy krathong", "victory",
        "champion*", "award*", "prize*", "record-breaking", "milestone",
        "grand opening", "inaugurat*", "gold medal*", "jubilee",
    ),
    "sport": (
        "sport*", "football*", "soccer", "tournament*", "championship*",
        "athlete*", "olympic*", "seagames", "league", "coach*", "striker",
        "fifa", "badminton", "volleyball", "marathon", "medal*",
    ),
}
_MOOD_RES = {
    tag: re.compile(r"\b(?:" + "|".join(
        re.escape(kw).replace(r"\*", r"\w*") for kw in kws) + r")\b", re.IGNORECASE)
    for tag, kws in _MOOD_LEXICON.items()
}


def _classify_moods(text: str) -> list[str]:
    """Mood tags present in the script — deterministic, lexicon order."""
    if not text:
        return []
    return [tag for tag, rex in _MOOD_RES.items() if rex.search(text)]


def pick_inf_look(text: str, forced: str | None = None) -> tuple[dict, dict]:
    """ONE style + ONE palette per article, drawn INDEPENDENTLY but always
    compatible (palette tone matches the style's bg_tone; both are
    mood-matched). Forced dropdown style id wins; the palette still rotates.
    Returns (style_copy, palette_copy), style annotated matched_moods +
    pick_source, palette annotated pick_source."""
    moods = _classify_moods(text)
    if forced and forced != "auto":
        s = _INF_STYLES_BY_ID.get(forced)
        if s is not None:
            pal = _pick_palette(moods, s["bg_tone"])
            return dict(s, matched_moods=moods, pick_source="forced"), pal
    pool = [s for s in _INF_STYLES if any(m in s["moods"] for m in moods)]
    source = "mood"
    if not pool:
        pool = [s for s in _INF_STYLES if "general" in s["moods"]]
        source = "general"
    style = dict(random.choice(pool), matched_moods=moods, pick_source=source)
    return style, _pick_palette(moods, style["bg_tone"])


# --- Motion catalog: ONE motion per BLOCK, kind-classified + mood-gated. ----
# kinds: stat | process | map | photo | hero (diorama ambient) | type (typography)
# moods: the 8 script-mood tags + "general" (pool-compatible fallback marker).
# tails describe ONLY internal-element motion — never the camera (loop safety).
_INF_MOTIONS: list[dict] = [
    # -- Statistic / data-driven
    {"id": "ticker", "label": "Count-up ticker", "kinds": ["stat"],
     "moods": ["business", "hard-news", "tech", "general"],
     "tail": "the hero figure rolls odometer-style: digits climb to the printed value, hold, then roll back and settle exactly on the printed figure"},
    {"id": "bars-grow", "label": "Sequential bar growth", "kinds": ["stat"],
     "moods": ["business", "hard-news", "general"],
     "tail": "the bars rise from the baseline one after another, hold at their printed heights, then ease back down and reset in the same order"},
    {"id": "chart-seq", "label": "Guided chart sequence", "kinds": ["stat"],
     "moods": ["business", "science", "tech", "general"],
     "tail": "chart elements appear one at a time in a fixed order, hold together, then fade out in reverse order and reset"},
    {"id": "pulse-points", "label": "Pulsing data points", "kinds": ["stat", "map"],
     "moods": ["tech", "science", "hard-news", "general"],
     "tail": "the data markers pulse in place, glowing brighter then dimmer in a slow rhythmic cycle, never changing position"},
    {"id": "ring-fill", "label": "Percentage ring fill", "kinds": ["stat"],
     "moods": ["business", "science", "general"],
     "tail": "the ring gauge fills around its circumference to the printed value, holds, then empties at the same pace and refills"},
    # -- Process / flow
    {"id": "cycle-rotate", "label": "Rotating cycle arc", "kinds": ["process"],
     "moods": ["science", "soft", "culture", "general"],
     "tail": "an arc rotates a full 360 degrees around the stages, each stage gently pulsing as the arc passes, returning seamlessly to its start angle"},
    {"id": "connectors-draw", "label": "Self-drawing connectors", "kinds": ["process"],
     "moods": ["tech", "business", "hard-news", "general"],
     "tail": "ink draws itself along the connector paths node to node, holds when complete, then un-draws back to the start and repeats"},
    {"id": "step-cards", "label": "Sequential step-card reveal", "kinds": ["process"],
     "moods": ["soft", "culture", "general"],
     "tail": "the step cards fade in one by one in order, hold together briefly, then fade out and the sequence restarts"},
    {"id": "timeline-spine", "label": "Timeline spine draw", "kinds": ["process"],
     "moods": ["culture", "business", "hard-news", "general"],
     "tail": "the timeline spine extends left to right while milestone dots pop in as it passes, holds, then rewinds back to the start"},
    {"id": "cascade-build", "label": "Cascade hierarchy build", "kinds": ["process"],
     "moods": ["tech", "business", "general"],
     "tail": "nodes land top-down layer by layer, connectors drawing as they go, holds, then reverses upward and rebuilds"},
    {"id": "arrow-flow", "label": "Flowing chevrons", "kinds": ["process"],
     "moods": ["hard-news", "business", "general"],
     "tail": "chevrons flow steadily along the path in one direction, continuous conveyor motion with no jumps"},
    # -- Map / geography
    {"id": "region-glow", "label": "Region glow", "kinds": ["map"],
     "moods": ["hard-news", "science", "general"],
     "tail": "region markers glow and fade one after another in a slow rhythmic sequence, borders staying perfectly still"},
    {"id": "route-draw", "label": "Route draw", "kinds": ["map"],
     "moods": ["soft", "culture", "business", "general"],
     "tail": "the route line draws itself across the map from origin to destination, holds, then erases back and redraws"},
    {"id": "locator-pulse", "label": "Locator pin pulse", "kinds": ["map"],
     "moods": ["hard-news", "soft", "general"],
     "tail": "the locator pin drops once, then pulses soft concentric rings that expand and fade, the map itself perfectly still"},
    # -- Photo-based
    {"id": "parallax-drift", "label": "2.5D parallax drift", "kinds": ["photo", "hero"],
     "moods": ["culture", "soft", "general"],
     "tail": "foreground and background layers drift a few pixels in opposite directions in depth, then ease back to the exact original alignment"},
    {"id": "cinemagraph", "label": "Cinemagraph", "kinds": ["photo", "hero"],
     "moods": ["culture", "soft", "general"],
     "tail": "one small element moves in a gentle repeating motion while everything else stays perfectly frozen"},
    {"id": "grid-morph", "label": "Grid morph", "kinds": ["photo"],
     "moods": ["culture", "sport", "celebration", "general"],
     "tail": "grid tiles crossfade and flip in a repeating sequence, each tile returning to its printed image before the cycle restarts"},
    # -- Hero / diorama ambient (our PNG heroes are isometric dioramas)
    {"id": "light-sweep", "label": "Light sweep", "kinds": ["hero"],
     "moods": ["tech", "celebration", "business", "general"],
     "tail": "a soft band of light sweeps slowly across the isometric diorama, dims, and sweeps again in the same direction"},
    {"id": "float-bob", "label": "Floating hero", "kinds": ["hero"],
     "moods": ["tech", "soft", "science", "general"],
     "tail": "the hero element bobs gently up and down in place, its soft shadow contracting and expanding in sync"},
    {"id": "breathing-glow", "label": "Breathing glow", "kinds": ["hero"],
     "moods": ["science", "culture", "tech", "general"],
     "tail": "the hero's ambient glow breathes brighter and dimmer in a slow steady rhythm, the artwork itself perfectly still"},
    {"id": "particles-drift", "label": "Particle drift", "kinds": ["hero"],
     "moods": ["celebration", "soft", "tech", "general"],
     "tail": "tiny ambient particles drift slowly upward through the scene, fading out as they respawn below, density constant"},
    {"id": "cloud-drift", "label": "Cloud shadow drift", "kinds": ["hero"],
     "moods": ["soft", "science", "culture", "general"],
     "tail": "soft cloud shadows drift slowly across the diorama from one edge to the other, wrapping around seamlessly"},
    {"id": "water-ripple", "label": "Water ripple", "kinds": ["hero"],
     "moods": ["soft", "science", "general"],
     "tail": "water surfaces ripple gently in place, concentric waves expanding and fading, the shoreline fixed"},
    {"id": "flag-wave", "label": "Flag wave", "kinds": ["hero"],
     "moods": ["hard-news", "sport", "celebration", "general"],
     "tail": "flags and cloth elements wave in a steady breeze, fabric ripples traveling smoothly corner to corner"},
    {"id": "steam-rise", "label": "Steam and smoke", "kinds": ["hero"],
     "moods": ["soft", "culture", "general"],
     "tail": "steam or smoke rises steadily and dissipates as it climbs, the source point fixed, density constant"},
    {"id": "crowd-sway", "label": "Crowd sway", "kinds": ["hero"],
     "moods": ["sport", "celebration", "culture", "general"],
     "tail": "tiny figures in the scene sway and shift weight subtly in place, never leaving their positions"},
    {"id": "traffic-flow", "label": "Traffic flow", "kinds": ["hero"],
     "moods": ["business", "hard-news", "general"],
     "tail": "vehicles flow steadily along the roads and wrap from exit back to entry, density and pace constant"},
    {"id": "energy-pulse", "label": "Energy pulse", "kinds": ["hero", "process"],
     "moods": ["tech", "science", "general"],
     "tail": "pulses of light travel along the circuit-like lines and wrap from end back to start, evenly spaced"},
    {"id": "weather-fall", "label": "Rain and snow", "kinds": ["hero"],
     "moods": ["hard-news", "science", "general"],
     "tail": "rain or snow falls steadily through the scene at a constant angle and pace, wrapping top to bottom seamlessly"},
    {"id": "day-night-shift", "label": "Day-night light cycle", "kinds": ["hero"],
     "moods": ["culture", "science", "soft", "general"],
     "tail": "the scene's lighting shifts slowly from warm daylight to cool dusk and back, color temperature only, no geometry moves"},
    {"id": "hologram-scan", "label": "Hologram scanline", "kinds": ["hero"],
     "moods": ["tech", "general"],
     "tail": "a faint scanline sweeps down over the hero once per cycle, leaving a brief subtle glow in its wake"},
    {"id": "shimmer-detail", "label": "Detail shimmer", "kinds": ["hero", "stat"],
     "moods": ["celebration", "tech", "general"],
     "tail": "metallic and glass details catch a subtle glint one after another, each settling before the next begins"},
    # -- Documentary atmosphere (slow, serious)
    {"id": "archival-flicker", "label": "Archival flicker", "kinds": ["hero", "photo"],
     "moods": ["hard-news", "culture", "general"],
     "tail": "exposure flickers very gently like archived film, brightness varying a few percent in an irregular but looping rhythm"},
    {"id": "dust-motes", "label": "Dust motes", "kinds": ["hero"],
     "moods": ["culture", "science", "soft", "general"],
     "tail": "dust motes float through the light beams in slow drifting motion, wrapping at the edges seamlessly"},
    {"id": "ember-drift", "label": "Ember drift", "kinds": ["hero"],
     "moods": ["hard-news", "general"],
     "tail": "embers or sparks drift slowly upward and fade, respawning below, density constant"},
    {"id": "haze-drift", "label": "Haze layers", "kinds": ["hero"],
     "moods": ["hard-news", "science", "general"],
     "tail": "translucent haze layers drift slowly at different speeds, wrapping horizontally, silhouettes fixed"},
    {"id": "light-rays", "label": "Light rays", "kinds": ["hero"],
     "moods": ["culture", "science", "general"],
     "tail": "volumetric light rays intensify and soften in a slow breathing cycle, their direction fixed"},
    # -- Typography / editorial
    {"id": "quote-reveal", "label": "Quote word reveal", "kinds": ["type"],
     "moods": ["culture", "hard-news", "general"],
     "tail": "the key phrase reveals word by word with a soft fade, holds complete, then fades whole and re-reveals"},
    {"id": "underline-sweep", "label": "Underline sweep", "kinds": ["type", "stat"],
     "moods": ["hard-news", "business", "general"],
     "tail": "an accent underline sweeps in beneath the key phrase from left to right, holds, then retracts and sweeps again"},
    {"id": "headline-sheen", "label": "Headline sheen", "kinds": ["type"],
     "moods": ["celebration", "sport", "business", "general"],
     "tail": "a subtle sheen sweeps across the headline text once per cycle, the letters never moving"},
]
_INF_MOTIONS_BY_ID = {m["id"]: m for m in _INF_MOTIONS}

_KIND_RES = {
    "stat": re.compile(
        r"\d|\bpercent\b|%|\bbaht\b|\bdollar|\bbillion|\bmillion|\btrillion\b"
        r"|\brose\b|\bfell\b|\bgrew\b|\bdrop|\bincrease|\bdecrease|\bdecline\b"
        r"|\bsurge|\brate\b|\baverage\b|\btotal\b", re.IGNORECASE),
    "process": re.compile(
        r"\bstep\b|\bphase\b|\bstage\b|\bprocess\b|\broadmap\b|\btimeline\b"
        r"|\bfirst\b|\bsecond\b|\bthird\b|\bfinally\b|\bbegan\b|\bthen\b"
        r"|\bplan\b|\bprocedure\b", re.IGNORECASE),
    "map": re.compile(
        r"\bprovince|\bregion\b|\bdistrict\b|\bnorth\b|\bsouth\b|\beast\b"
        r"|\bwest\b|\bborder|\bmap\b|\blocated\b|\bcoast\b|\briver\b"
        r"|\bisland\b|\barea of\b|\bkm2\b|\bsquare kilometer", re.IGNORECASE),
    "photo": re.compile(
        r"\bfootage\b|\bphoto\b|\bphotograph|\bcamera\b|\bvideo shows\b"
        r"|\bclip shows\b", re.IGNORECASE),
    "type": re.compile(r"[\u201c\u201d\"]|\bslogan\b|\bquote\b|\bmotto\b", re.IGNORECASE),
}


def _classify_block_kinds(block: str) -> list[str]:
    """Content kinds present in one infographic block — deterministic,
    lexicon order. Empty list → caller falls back to the hero pool."""
    if not block:
        return []
    return [kind for kind, rex in _KIND_RES.items() if rex.search(block)]


def pick_inf_motion(block: str, article_moods: list[str],
                    forced: str | None = None) -> dict:
    """ONE motion per BLOCK: content-kind classified, mood-gated by the block
    AND its article (documentary-slow vs news-urgent). Forced dropdown id
    wins. Returns motion copy annotated matched_kinds + pick_source."""
    if forced and forced != "auto":
        m = _INF_MOTIONS_BY_ID.get(forced)
        if m is not None:
            return dict(m, matched_kinds=_classify_block_kinds(block),
                        pick_source="forced")
    kinds = _classify_block_kinds(block)
    mood_pool = set(article_moods) | set(_classify_moods(block))
    pool = [m for m in _INF_MOTIONS
            if any(k in m["kinds"] for k in kinds)
            and any(mo in m["moods"] for mo in mood_pool)]
    source = "mood"
    if not pool:
        pool = [m for m in _INF_MOTIONS if any(k in m["kinds"] for k in kinds)]
        source = "kind"
    if not pool:
        pool = [m for m in _INF_MOTIONS if "general" in m["moods"]]
        source = "general"
    return dict(random.choice(pool), matched_kinds=kinds, pick_source=source)


# De-jerk loop cure (lab-verified 2026-09-02 on real Veo 8 s clips, 720+1080):
# raw loop seam = ~55x normal frame-to-frame motion; this recipe cuts it to
# ~2x. The bridge dissolves the clip's tail into the first 0.5 s played
# BACKWARD, so it lands exactly on frame 0; duration=0.5-1/24 lets the fade
# COMPLETE on the last bridge frame (no residual tail contamination).
# Verified live: output stays exactly 8.0 s at both resolutions.
_LOOP_CROSSFADE_CMD = (
    'ffmpeg -i loop.mp4 -filter_complex "[0:v]split=3[a][b][c];'
    "[a]trim=end=7.5,setpts=PTS-STARTPTS[a1];"
    "[b]trim=start=7.5,setpts=PTS-STARTPTS[b1];"
    "[c]trim=end=0.5,setpts=PTS-STARTPTS,reverse[cr];"
    "[b1][cr]xfade=transition=fade:duration=0.458333:offset=0[xf];"
    '[a1][xf]concat=n=2:v=1:a=0[out]" -map "[out]" -an loop-smooth.mp4'
)


def _loop_prompt(story_line: str, figures: str, motion: dict) -> str:
    """Full Flow-ready loop txt for ONE infographic PNG: engine line, camera
    lock, the block's motion tail, loop-closure contract, post crossfade."""
    return (
        f"SEAMLESS LOOP PROMPT — {story_line}\n"
        f"MOTION STYLE: {motion['label']} ({motion['id']}, {motion['pick_source']}) — "
        "recommended for this block's content and mood.\n"
        f"FIGURES (verbatim, never re-render): {figures}\n\n"
        "ENGINE: Flow → Frames-to-Video. Use this exact PNG as BOTH the first and "
        "the last frame. Veo 3.1 (Lite or Fast), 8 s, 720p, 16:9. Omni Flash has NO "
        "last-frame input — loops are not guaranteed on it.\n\n"
        "----- PASTE FROM HERE INTO FLOW -----\n"
        "PERFECTLY STATIC TRIPOD. LOCKED FRAME. ZERO camera movement, zero zoom, "
        "zero pan, zero drift. Layout, scale, background gradient/pattern, and "
        "typography are frozen 100% from start to end.\n"
        f"ONLY pre-existing internal elements move: {motion['tail']}.\n"
        "The motion is cyclical: it eases out and settles back to the exact starting "
        "state before the clip ends — first frame = last frame, pixel-identical, "
        "minimal motion near the loop point.\n"
        "No new elements, no text changes, no invented figures — printed numbers "
        "stay exactly as shown. The bottom strip of the frame stays frozen.\n"
        "----- END PROMPT -----\n\n"
        "POST FIX (only if the loop point still jitters — dissolve the end into "
        "a REVERSED copy of the start so the bridge lands exactly on frame 0; "
        "for 8 s clips):\n"
        f"  {_LOOP_CROSSFADE_CMD}\n"
        "  (trim points assume 8 s — for other lengths use duration−0.5 as the "
        "head and blend the final 0.5 s reversed, fade duration 0.458333)"
    )


_TV_SAFE_SPEC = (
    "- All text inside the central 80% of the frame; no text near edges.\n"
    "- Stat/number type >= 8% of frame height; sans-serif; contrast >= 4.5:1; light-on-dark preferred.\n"
    "- <= 3 highlighted stats and <= 5 short labels; readable in six seconds on a TV screen.\n"
    "- Background: NEVER a plain solid fill. Use a SUBTLE gradient or a minimal geometric abstract "
    "(thin-line grid, soft shapes, faint texture) — low contrast, low detail, never competing with the text; "
    "demand generous whitespace (the generator's default instinct is to fill every pixel).\n"
    "- One central HERO element: a 3D isometric object, scene, or miniature diorama depicting THIS block's "
    "actual subject (the real landmark, building, vehicle, or scene from the block) — clean and uncluttered; "
    "it is the focal point of the frame.\n"
    "- Single centered focal composition (loop-friendly), suitable as the base image for a seamless loop animation.\n"
    "- Even ambient lighting: no directional key light, no cast shadows beyond the diorama base, no lens flare, "
    "no vignette, no film grain, no baked-in particles (these break loop animation).\n"
    "- English-only labels. Figures VERBATIM from the marked block only — never invented, rounded, or added from the wider script.\n"
    "- Keep the bottom 2% strip of the frame free of content — background only. (Reserved for the watermark crop; no text, icons, or key art may live there.)\n"
    "- ABSOLUTE: no logos, no government seals or emblems, no TV channel branding, no watermarks. We are the source; stay with the given context."
)


def _template_brief(block: str, style: dict, palette: dict) -> str:
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', block.strip()) if s.strip()]
    digit_sentences = [s for s in sentences if re.search(r'\d', s)]
    if digit_sentences:
        figures_text = ", ".join(digit_sentences)
    else:
        figures_text = block.strip()[:400]

    art = style.get("art", "")
    return (
        f"{art}\n"
        f"PALETTE (LOCKED): {_palette_desc(palette)}\n\n"
        f"Show ONLY these figures (verbatim): {figures_text}\n\n"
        f"{_TV_SAFE_SPEC}"
    )


async def _compose_briefs(script: str, blocks: list[str],
                          locked_style: dict, locked_palette: dict) -> list[dict]:
    """One style + one palette LOCKED per ARTICLE; the LLM only art-directs
    each block's composition inside them. Any drift (wrong style id, bad JSON,
    wrong count) → template briefs in the SAME locked look, so every image in
    the set stays one visual family."""
    style_id = locked_style["id"]
    palette_line = _palette_desc(locked_palette)

    def _fallback_all() -> list[dict]:
        return [
            {"style": style_id, "brief": _template_brief(b, locked_style, locked_palette)}
            for b in blocks
        ]

    art = locked_style.get("art", "")
    system_prompt = (
        "You are an NBT World TV-news infographic art director. You are given a broadcast script and numbered infographic blocks.\n"
        f"The article's visual style is LOCKED to '{style_id}' — every block MUST use this exact style. Never substitute, blend, or drift to another style.\n"
        f"OPEN every brief with this exact art line: {art}\n"
        f"THEN on the next line: PALETTE (LOCKED): {palette_line}\n"
        "Then art-direct each block within it: which figures to show (verbatim from that block) and how to compose it. "
        "Each block's HERO must be a 3D isometric object, scene, or miniature diorama of THAT block's actual subject "
        "(the real place, building, vehicle, or scene it describes). Vary only the hero and composition between blocks — "
        "the style, palette and tone stay identical.\n\n"
        f"TV-SAFE SPEC:\n{_TV_SAFE_SPEC}\n\n"
        "Return ONLY a JSON array, one object per block in order:\n"
        '[{"style": "<style_id>", "brief": "<full art-direction brief>"}]\n'
        "Output ONLY raw JSON array. No markdown fences, no preamble, no commentary."
    )

    blocks_formatted = "\n\n".join(f"BLOCK {i + 1}:\n{b}" for i, b in enumerate(blocks))
    user_prompt = f"FULL SCRIPT:\n{script}\n\n{blocks_formatted}"

    try:
        raw_out = await zai.zai_message(
            user_prompt, system=system_prompt, max_tokens=4000, timeout=60
        )
        if not raw_out or not raw_out.strip():
            return _fallback_all()

        text_to_parse = raw_out.strip()
        if "```" in text_to_parse:
            m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text_to_parse)
            if m:
                text_to_parse = m.group(1).strip()

        data = json.loads(text_to_parse)
        if not isinstance(data, list) or len(data) != len(blocks):
            return _fallback_all()

        result: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                return _fallback_all()
            if item.get("style") != style_id:  # style drift → fall back as one set
                return _fallback_all()
            brief = item.get("brief")
            if not brief or not isinstance(brief, str) or not brief.strip():
                return _fallback_all()
            result.append({
                "style": style_id,
                "brief": brief.strip(),
            })
        return result
    except Exception:
        return _fallback_all()


def _crop_watermark(path: Path) -> None:
    """Shave bottom watermark strip losslessly, keeping full width."""
    from PIL import Image
    if not path.exists():
        raise FileNotFoundError(f"image not found: {path}")
    with Image.open(path) as img:
        w, h = img.size
        strip = max(28, round(h * 0.018))
        if h > strip:
            cropped = img.crop((0, 0, w, h - strip))
            cropped.save(path)


async def generate_infographics(text: str, style: str = "auto",
                                motion: str = "auto") -> dict:
    blocks, warnings = parse_inf_blocks(text)
    if not blocks:
        raise HTTPException(400, "no [inf] blocks — wrap a paragraph with [inf] … [inf/] first")

    # ONE style + ONE palette per article, drawn independently but tone- and
    # mood-compatible; reused for every block = one visual family on air.
    locked, palette = pick_inf_look(text, style)
    briefs = await _compose_briefs(text, blocks, locked, palette)
    article_moods = locked.get("matched_moods", [])

    non_empty = [line.strip() for line in text.splitlines() if line.strip()][:5]
    slug = ""
    for line in non_empty:
        m = re.search(r'EVE\w+', line)
        if m:
            slug = m.group(0)
            break
    if not slug:
        slug = f"untitled-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    date_str = datetime.now().strftime("%Y-%m-%d")
    out_dir = Path.home() / "Downloads" / "Newsroom Infographics" / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    files: list[dict] = []
    nid: str = ""
    notebook_deleted = False

    temp_md = Path(f"/tmp/newsroom-infographic-{slug}.md")
    try:
        temp_md.write_text(text, encoding="utf-8")
        title = f"NEWSROOM · {slug} · {date_str}"
        create_res = await _script(["notebooklm", "create", title, "--json"], timeout=60)
        nid = str((create_res.get("notebook") or create_res).get("id")
                  or create_res.get("notebook_id") or "")
        if not nid:
            errors.append("failed to create notebook (no notebook ID returned)")
        else:
            rc_src, _, err_src = await _run([
                "notebooklm", "source", "add", str(temp_md),
                "-n", nid, "--title", f"{slug} script", "--json",
            ], timeout=60)
            if rc_src != 0:
                errors.append(f"failed to add script source: {err_src.decode(errors='replace')}")

            first_line = non_empty[0] if non_empty else slug
            preset = locked.get("preset", "professional")
            for i, (block, brief_item) in enumerate(zip(blocks, briefs)):
                brief_text = brief_item.get("brief", "")

                png_name = f"{slug}-inf{i + 1}.png"
                png_path = out_dir / png_name
                loop_name = f"{slug}-inf{i + 1}-flowlab-loop-prompt.txt"
                loop_path = out_dir / loop_name

                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', block.strip()) if s.strip()]
                digit_sentences = [s for s in sentences if re.search(r'\d', s)]

                def plain(s: str) -> str:
                    # loop txt is plain prose — no **/~~ markers
                    return re.sub(r'\*+|~+', '', s)

                block_figures = (", ".join(plain(s) for s in digit_sentences)
                                 if digit_sentences else plain(block)[:200])

                # ONE motion per BLOCK: kind + mood picked, forced dropdown wins.
                motion_rec = pick_inf_motion(block, article_moods, motion)
                loop_content = _loop_prompt(first_line, block_figures, motion_rec)

                gen_argv = [
                    "notebooklm", "generate", "infographic",
                    "-n", nid,
                    "--orientation", "landscape",
                    "--detail", "standard",
                    "--style", preset,
                    "--wait", "--json",
                    "--prompt-file", "-",
                ]
                try:
                    await _script(gen_argv, timeout=180, stdin=brief_text.encode("utf-8"))
                except Exception as e:
                    errors.append(f"block {i + 1} generate failed: {e}")
                    continue

                dl_argv = [
                    "notebooklm", "download", "infographic",
                    "-n", nid,
                    "--latest",
                    "--force",
                    str(png_path),
                ]
                try:
                    rc_dl, out_dl, err_dl = await _run(dl_argv, timeout=60)
                    if rc_dl != 0:
                        errors.append(f"block {i + 1} download failed: {_fail(out_dl, err_dl)}")
                        continue
                except Exception as e:
                    errors.append(f"block {i + 1} download failed: {e}")
                    continue

                try:
                    _crop_watermark(png_path)
                except Exception as e:
                    errors.append(f"block {i + 1} watermark crop failed: {e}")

                try:
                    loop_path.write_text(loop_content, encoding="utf-8")
                except Exception as e:
                    errors.append(f"block {i + 1} loop prompt write failed: {e}")

                files.append({
                    "png": str(png_path),
                    "loop_prompt": str(loop_path),
                    "filename": png_name,
                    "loop_filename": loop_name,
                    "rel_png": f"{slug}/{png_name}",
                    "rel_loop_prompt": f"{slug}/{loop_name}",
                    "motion": {
                        "id": motion_rec["id"],
                        "label": motion_rec["label"],
                        "kinds": motion_rec["matched_kinds"],
                        "pick_source": motion_rec["pick_source"],
                    },
                })
    except HTTPException as e:
        # advisory pipeline: CLI failures (create 502, source-add 504, …) are
        # error entries, never escapes — never raise past step 1 (validator 2026-08-31)
        errors.append(f"infographic pipeline error: {e.detail}")
    except Exception as e:
        errors.append(f"infographic pipeline error: {e}")
    finally:
        if temp_md.exists():
            try:
                temp_md.unlink()
            except Exception:
                pass
        if nid:
            try:
                rc_del, _, err_del = await _run(["notebooklm", "delete", "-y", "-n", nid], timeout=60)
                notebook_deleted = (rc_del == 0)
                if not notebook_deleted:
                    errors.append(f"failed to delete notebook {nid}: {err_del.decode(errors='replace')}")
            except Exception as e:
                errors.append(f"failed to delete notebook {nid}: {e}")

    return {
        "slug": slug,
        "dir": str(out_dir),
        "blocks": len(blocks),
        "style": {
            "id": locked["id"],
            "label": locked.get("label", locked["id"]),
            "preset": locked.get("preset", ""),
            "matched_moods": locked.get("matched_moods", []),
            "pick_source": locked.get("pick_source", "mood"),
        },
        "palette": {
            "id": palette["id"],
            "name": palette["name"],
            "tone": palette["tone"],
            "accent": palette["accent"],
            "bg": palette["bg"],
            "pick_source": locked.get("pick_source", "mood"),
        },
        "files": files,
        "notebook_deleted": notebook_deleted,
        "errors": errors,
        "warnings": warnings,
    }


@router.post("/api/newsroom/infographic/generate")
async def api_newsroom_infographic_generate(body: dict = Body(...)) -> dict:
    return await generate_infographics(
        text=body.get("text", ""),
        style=body.get("style", "auto"),
        motion=body.get("motion", "auto"),
    )


@router.get("/api/newsroom/infographic/file")
def api_newsroom_infographic_file(f: str) -> FileResponse:
    base = (Path.home() / "Downloads" / "Newsroom Infographics").resolve()
    if not (f.endswith(".png") or f.endswith(".txt")):
        raise HTTPException(403, "only .png and .txt files are allowed")
    rp = (base / f).resolve()
    if not rp.is_relative_to(base):
        raise HTTPException(403, "path escapes base directory")
    if not rp.is_file():
        raise HTTPException(404, "file not found")
    media_type = "image/png" if f.endswith(".png") else "text/plain"
    return FileResponse(rp, media_type=media_type)
