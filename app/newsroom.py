"""Newsroom panel — thin subprocess wrapper around the newsroom skill scripts
(``queue.py`` + ``nl_append.py``) in the Cephalon vault.

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
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from . import zai

router = APIRouter()

SCRIPTS = Path.home() / "Cephalon" / "10-knowledge" / "skills" / "newsroom" / "scripts"
QUEUE = SCRIPTS / "queue.py"
APPEND = SCRIPTS / "nl_append.py"
RADIO = SCRIPTS / "radio.py"
# The Rules Gem drives REWRITE (news-producer prompt → two-layer broadcast
# script). ~/Gems is the office canonical copy; home has no ~/Gems, so the
# vault-synced gem is the source here — _gem_text falls through to it.
BEN_GEM = Path(__file__).parent / "gems" / "radio-news-rewrite.md"
SEO_GEM = Path.home() / "Cephalon" / "10-knowledge" / "ai-workflow" / "gemini-gem-thailandnow-seo.md"
# Run via the system interpreter, not the scripts' shebang: vault files carry no
# exec bit (git syncs can drop it — the Somatic original 500s on exactly this),
# and the skill's deps live with the system python3, not Railjack's venv.
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
    argv = [PY, str(QUEUE), "list", "--json", "--author", author]
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
    """Drop a finished script beneath ***END CREDIT*** in the day's NL RUNDOWN
    tab. `nl_append.py` resolves the doc (--today via Drive, or explicit
    --doc) and needs the google-workspace MCP OAuth creds on this machine."""
    text = body.get("text", "")
    if not text.strip():
        raise HTTPException(400, "text required")
    argv = [PY, str(APPEND)]
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
    Calls ``nl_append.py fill --tab ... --slot N --today/--doc ... --text ...``.
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

    Body: {text, year, month, day, section (AM/MIDDAY/EVE),
           block (NATIONAL/GLOBAL/BUSINESS), slot (int)}
    Calls ``radio.py fill --year ... --month ... --day ... --section ...
    --block ... --slot ... --text ...``.
    400 on any missing required field.
    """
    required = ("year", "month", "day", "section", "block", "slot")
    missing = [f for f in required if body.get(f) is None]
    if missing:
        raise HTTPException(400, "missing required fields: %s" % ", ".join(missing))
    try:
        year = int(body["year"])
        month = int(body["month"])
        day = int(body["day"])
        slot = int(body["slot"])
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "year/month/day/slot must be integers: %s" % e)
    section = str(body["section"]).upper()
    block = str(body["block"]).upper()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    argv = [
        PY, str(RADIO), "fill",
        "--year", str(year), "--month", str(month), "--day", str(day),
        "--section", section, "--block", block, "--slot", str(slot),
        "--text", text,
    ]
    return await _script(argv, timeout=60)



# ---------------------------------------------------------------- rewrite
# Source article → two-layer broadcast script via the news-producer Rules Gem.
# Rides app/zai.py (the OmniRoute gateway, NOT z.ai direct), so the pass keeps
# working past a z.ai quota wall. Editorial hard rule: source-only, and never
# translate a person's name or title out of the original Thai — the writers
# render those themselves.


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
        + "\n\n=== OUTPUT OVERRIDE (replaces JSON instructions) ===\n"
        "Output the broadcast rewrite as readable prose (Ben's hard rules and voice still apply). "
        "Do NOT output JSON. Wrap every person's NAME in **double-stars** per the NAME OVERLAY rule below. "
        "Wrap every date, time, and relative-time expression in -/…/- markers "
        "(e.g. -/July 15, 2026/-, -/3:00 PM/-, -/next month/-). These become underlined in the Doc. "
        "These are the ONLY allowed markup. Output ONLY the rewritten broadcast prose. No JSON, no preamble, no commentary.\n\n"
        "=== NAME OVERLAY RULE ===\n"
        "For each person the SOURCE names, render their name as follows:\n"
        "- If you can CONFIDENTLY confirm that person's official English name (the established public "
        "rendering — e.g. a minister's known English spelling): output **[OfficialEnglish(Thai)]** "
        "(e.g. **[Anutin Charnvirakul(อนุทิน ชาญวีรกูล)]**). "
        "Keep the person's rank/title in the ORIGINAL THAI SCRIPT exactly as the source gives it.\n"
        "- If you CANNOT confidently confirm an official English name: output **Thai name** as-is "
        "(bold-marked, NO transliteration, NO guessing — e.g. **นายกฯ**). Editors fix gaps.\n"
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
        "Produce ONLY the AI SEO Block — Version A (40-60w summary) + Version B (key points).\n"
        "SKIP focus keyphrases, meta descriptions, and hashtags entirely. Do not output keyphrase lists or hashtags.\n"
        "Output ONLY the AI SEO Block (Version A and Version B)."
    )
    out_ben, out_seo = await asyncio.gather(
        zai.zai_message(ben_prompt, max_tokens=9000, timeout=120),
        zai.zai_message(text, system=seo_system, max_tokens=4000, timeout=60),
    )
    if not out_ben.strip():
        raise HTTPException(502, "rewrite came back empty")
    if not out_seo.strip():
        raise HTTPException(502, "seo generation came back empty")
    return {"rewritten": out_ben, "seo": out_seo}


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
