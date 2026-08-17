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
import re
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from . import zai

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


async def _run(argv: list[str], timeout: float = 90) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
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


async def _script(argv: list[str], timeout: float = 90):
    rc, out, err = await _run(argv, timeout=timeout)
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
    text = body.get("text", "")
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
    text = body.get("text", "")
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
    text = (body.get("text") or "").strip()
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
# working past a z.ai quota wall. Editorial hard rule: source-only, and never
# translate a person's name or title out of the original Thai — the writers
# render those themselves.


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
    # Tidy: empty parens from multi-word Thai stripped separately, then orphaned brackets
    body = re.sub(r'\(\s*\)', '', body)
    body = re.sub(r'\[([^\[\]()]+)\]', r'\1', body)
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
        "- **double-stars** and ~~tildes~~ are the ONLY markup allowed in `body`; no other markdown.\n\n"
        "=== NAME OVERLAY RULE ===\n"
        "For each person the SOURCE names, render their name as follows:\n"
        "- If the SOURCE gives the name in ENGLISH: use that English name exactly as written, bolded "
        "(**EnglishName**). Do NOT generate or invent Thai script for an English-source name.\n"
        "- If the SOURCE gives the name in THAI and you can CONFIDENTLY confirm the official English "
        "rendering: output **[OfficialEnglish(Thai)]** (e.g. **[Anutin Charnvirakul(อนุทิน ชาญวีรกูล)]**).\n"
        "- If the SOURCE gives the name in THAI and you CANNOT confirm an official English name: "
        "output **Thai name** as-is (bold-marked, NO transliteration, NO guessing — e.g. **นายกฯ**).\n"
        "NEVER transliterate, romanize, or invent a Thai rendering for a name the source does not "
        "contain in Thai. When in doubt, copy the source's name verbatim. Editors fix gaps.\n"
        "NARROW CARVE-OUT: knowledge is allowed ONLY to supply a named person's official English "
        "name-form. Never use knowledge to ADD names, dates, figures, events, or any other facts — "
        "all other content is SOURCE-ONLY.\n\n"
        "=== CRITICAL EDITORIAL RULE (overrides everything above) ===\n"
        "Use ONLY the information in the SOURCE ARTICLE below. Never add "
        "dates, ranks, titles, agencies, figures, locations, or any fact from your "
        "own knowledge or training. Specifically:\n"
        "- Copy each person's rank/title EXACTLY as the source gives it — never "
        "promote, demote, or infer one (source says Prime Minister → not Deputy).\n"
        "- Do NOT invent a day of week, absolute date, or which agency acts unless "
        "the source states it. No date in source → write none.\n"
        "- Do NOT guess transliterations. Official English names only (per NAME OVERLAY above), "
        "else Thai-only fallback.\n\n"
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
    # Reassemble the sendable blob the panel already understands: EN/TH title
    # lines, then the marked-up body. Downstream (Script box, SEND TO NL/RADIO)
    # is unchanged — the JSON is only a more reliable transport for the pieces.
    rewritten = f"EN: {title}\nTH: {title_th}\n\n{body}" if (title or title_th) else body
    return {"rewritten": rewritten, "seo": out_seo}


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


@router.post("/api/newsroom/rewrite/convert")
async def rewrite_convert() -> dict:
    """FREE IDE CONVERT — relay the Antigravity rewrite handoff (``_REWRITE_HANDOFF`` JSON) as
    ``{rewritten, seo}``, the SAME shape ``api_rewrite`` returns so the frontend path is identical.
    No LLM, no body. Soft-fails to HTTP 200 ``{rewritten:"", seo:"", errors:[...]}`` when the
    handoff is missing/unparseable — points the user at 📋 IDE REWRITE first."""
    miss = {"rewritten": "", "seo": "", "errors": ["no IDE handoff file — run 📋 IDE REWRITE first"]}
    if not _REWRITE_HANDOFF.exists():
        return miss
    try:
        data = json.loads(_REWRITE_HANDOFF.read_text(encoding="utf-8"))
    except Exception:
        return miss
    return {
        "rewritten": (data.get("rewritten") or "").strip(),
        "seo": (data.get("seo") or "").strip(),
        "errors": [],
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
