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

router = APIRouter()

SCRIPTS = Path.home() / "Cephalon" / "10-knowledge" / "skills" / "newsroom" / "scripts"
QUEUE = SCRIPTS / "queue.py"
APPEND = SCRIPTS / "nl_append.py"
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


async def _script(argv: list[str], timeout: float = 90):
    rc, out, err = await _run(argv, timeout=timeout)
    if rc != 0:
        raise HTTPException(502, err.decode(errors="replace")[-300:])
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
