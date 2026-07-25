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
# The Rules Gem drives REWRITE (news-producer prompt → two-layer broadcast
# script). ~/Gems is the office canonical copy; home has no ~/Gems, so the
# vault-synced gem is the source here — _gem_text falls through to it.
GEM = Path.home() / "Gems" / "news-producer-gem.md"
GEM_FALLBACK = Path.home() / "Cephalon" / "10-knowledge" / "ai-workflow" / "gemini-gem-news-rules.md"
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


# ---------------------------------------------------------------- rewrite
# Source article → two-layer broadcast script via the news-producer Rules Gem.
# Rides app/zai.py (the OmniRoute gateway, NOT z.ai direct), so the pass keeps
# working past a z.ai quota wall. Editorial hard rule: source-only, and never
# translate a person's name or title out of the original Thai — the writers
# render those themselves.


def _gem_text() -> str:
    for p in (GEM, GEM_FALLBACK):
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise HTTPException(500, "Rules Gem not found (looked in ~/Gems and the vault)")


@router.post("/api/newsroom/rewrite")
async def api_rewrite(body: dict = Body(...)):
    """Run the Script-box text through the Rules Gem → finished two-layer script.

    The override block restates the editorial non-negotiables on top of the gem:
    source-only (no fact from training memory), and every person's name +
    title/rank stays in the original Thai script. Returns ``{"rewritten": ...}``
    for the panel's iframe."""
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "nothing to rewrite — the Script box is empty")
    prompt = (
        _gem_text()
        + "\n\n=== CRITICAL EDITORIAL RULE (overrides everything above) ===\n"
        "Use ONLY the information in the SOURCE ARTICLE below. Never add names, "
        "dates, ranks, titles, agencies, figures, locations, or any fact from your "
        "own knowledge or training. Specifically:\n"
        "- Copy each person's rank/title EXACTLY as the source gives it — never "
        "promote, demote, or infer one (source says Prime Minister → not Deputy).\n"
        "- Do NOT invent a day of week, absolute date, or which agency acts unless "
        "the source states it. No date in source → write none.\n"
        "- Do NOT guess transliterations. If the source doesn't state it, omit it.\n"
        "- Do NOT translate or transliterate any PERSON'S NAME or their TITLE/rank "
        "from Thai — leave every name and honorific in the ORIGINAL THAI SCRIPT "
        "exactly as the source writes it. Translate the rest of the story into "
        "English as normal; the human writers render the names/titles themselves.\n"
        "Output ONLY the finished two-layer script (broadcast layer, then `---`, "
        "then the digital block). No preamble, no commentary.\n\n"
        "=== SOURCE ARTICLE ===\n" + text
    )
    out = await zai.zai_message(prompt, max_tokens=9000, timeout=120)
    if not out.strip():
        raise HTTPException(502, "rewrite came back empty")
    return {"rewritten": out}


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
