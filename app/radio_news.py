"""RADIO ▸ News Fill — thin subprocess wrapper around the radio-news skill
script (``radio_news.py``) in the skill-library repo (synced to ~/.claude/skills/).

Mirrors ``app/newsroom.py``: the CLI script is the contract (it owns the
Drive Docs batchUpdate over the 3 radio tabs), this panel only builds argv,
feeds the write path its pieces/slice on stdin, and surfaces ``_fatal``
(→ 400) vs stderr (→ 502). argv **lists** via
``asyncio.create_subprocess_exec`` (never shell).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

router = APIRouter()

SCRIPTS = Path.home() / ".claude" / "skills" / "newsroom" / "scripts"
RNEWS = SCRIPTS / "radio_news.py"
DFORMAT = SCRIPTS / "doc_format.py"
# Editor Ben's broadcast-rewrite gem, sliced by the fill script at write time.
# Passed explicitly (env) so the script never has to guess the repo location.
REWRITE_GEM = Path(__file__).parent / "gems" / "radio-news-rewrite.md"
# Person-name tagger gem for the reusable publication-formatting pass.
FORMAT_GEM = Path(__file__).parent / "gems" / "doc-format-entities.md"
# See newsroom.py: run via system python3 (skill deps live there, not in Railjack's venv).
PY = "python3"


async def _run(argv: list[str], timeout: float = 90,
               stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    kwargs: dict = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
    if stdin is not None:
        kwargs["stdin"] = asyncio.subprocess.PIPE
    # Inherit the hub env; point each child at the gem(s) it slices. Passing
    # both is harmless — each script reads only the var it needs.
    kwargs["env"] = {**os.environ,
                     "RADIO_REWRITE_GEM": str(REWRITE_GEM),
                     "DOC_FORMAT_GEM": str(FORMAT_GEM)}
    proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "radio news script timed out")
    return proc.returncode or 0, out, err


def _json(out: bytes):
    """Parse script stdout; a ``_fatal`` payload is the script reporting a
    user-visible failure (missing handoff, bad doc) → 400 with its message."""
    data = json.loads(out)
    if isinstance(data, dict) and data.get("_fatal"):
        raise HTTPException(400, data["_fatal"])
    return data


def _fail(out: bytes, err: bytes) -> str:
    """Best error text for a nonzero exit. Scripts print their own reason as
    ``{"_fatal": ...}`` on STDOUT, so check stdout first — else a blank stderr
    would read 'script failed (no output)'."""
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


# ---------------------------------------------------------------- docs / report


@router.get("/api/newsroom/radio/news/docs")
async def api_docs(parent: str | None = None, limit: int | None = None):
    """Doc picker: ``list-docs`` over the radio parent folder. Optional
    ``?parent=``/``?limit=`` pass straight through."""
    argv = [PY, str(RNEWS), "list-docs"]
    if parent:
        argv += ["--parent", parent]
    if limit is not None:
        argv += ["--limit", str(limit)]
    return await _script(argv)


@router.get("/api/newsroom/radio/news/browse")
async def api_browse(parent: str | None = None, all_docs: bool = False):
    """Folder browser: one level of the Drive tree under ``?parent=`` (default
    RT-2026). Returns ``{parent, folders, docs}`` — the panel drills folder by
    folder instead of loading every script doc at once. ``?all_docs=true``
    returns every child doc UNFILTERED (for the NL picker, whose "NL & NWB
    DDMMYY" docs don't match the radio NAME_RE filter)."""
    argv = [PY, str(RNEWS), "browse"]
    if parent:
        argv += ["--parent", parent]
    if all_docs:
        argv += ["--all"]
    return await _script(argv)


@router.get("/api/newsroom/radio/news/report")
async def api_report():
    """Read the scout handoff JSON the CONVERT button feeds the panel."""
    return await _script([PY, str(RNEWS), "report"])


# ---------------------------------------------------------------- apply (write)


@router.post("/api/newsroom/radio/news/apply")
async def api_apply(body: dict = Body(...)):
    """Write path — ``fill`` the picked doc's radio tabs. Pieces + slice go to
    the child on stdin; the 180s cap covers a Docs batchUpdate over 3 tabs."""
    doc_id, kind, category = body.get("doc_id"), body.get("kind"), body.get("category")
    if not (doc_id and kind and category):
        raise HTTPException(400, "doc_id, kind and category are required")
    argv = [PY, str(RNEWS), "fill", "--doc", str(doc_id),
            "--kind", str(kind), "--category", str(category)]
    stdin = json.dumps({"pieces": body.get("pieces", []),
                        "slice": body.get("slice")}).encode()
    return await _script(argv, timeout=180, stdin=stdin)


@router.post("/api/newsroom/radio/news/autofill")
async def api_autofill(body: dict = Body(...)):
    """AUTOPILOT — one click: the child reads the scout handoff itself and fills
    the doc with no human selection (pieces auto-placed by ``assign_pieces``,
    SEA leading each broadcast). No stdin; pairs with CHEAP SCOUT's exact-count
    sweep for max token economy. 180s covers a batchUpdate over 3 tabs."""
    doc_id, kind, category = body.get("doc_id"), body.get("kind"), body.get("category")
    if not (doc_id and kind and category):
        raise HTTPException(400, "doc_id, kind and category are required")
    argv = [PY, str(RNEWS), "autofill", "--doc", str(doc_id),
            "--kind", str(kind), "--category", str(category)]
    return await _script(argv, timeout=180)


# ---------------------------------------------------------------- format (polish)


@router.post("/api/newsroom/format/apply")
async def api_format(body: dict = Body(...)):
    """Publication polish — bold people names + underline dates across the doc's
    tabs (or one ``tab``). Reusable beyond RADIO; idempotent (safe re-run). Gem
    path is handed to the child via env. 120s covers a batchUpdate per tab."""
    doc_id = body.get("doc_id")
    if not doc_id:
        raise HTTPException(400, "doc_id is required")
    argv = [PY, str(DFORMAT), "--doc", str(doc_id)]
    tab = body.get("tab")
    if tab:
        argv += ["--tab", str(tab)]
    return await _script(argv, timeout=120)
