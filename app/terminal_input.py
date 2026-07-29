"""POST /api/terminal/insert — type literal text into the tmux session.

ttyd serves ``tmux new -A -s main`` writable; we inject via
``tmux send-keys -t <session> -l -- <text>`` (``-l`` = literal, ``--`` guards
text starting with ``-`` or ``/``). **Type-only**: newlines are rejected (400)
so nothing can auto-execute — Naz reviews and presses Enter in the pane.
>4000 chars rejected (raised from 500 — agent prompts like the HANDOFF meta run ~760). tmux runs as an argv list (never ``shell=True``).
"""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import CONFIG

router = APIRouter()

_MAX = 4000  # raised from 500 — agent prompts (HANDOFF meta ~760) need the room; tmux send-keys handles long argv fine


class InsertBody(BaseModel):
    text: str


def _tmux_session() -> str:
    """Session name from the tmux module's ``options.tmux_session`` (default main)."""
    for m in CONFIG.modules:
        if m.id == "tmux":
            return (m.options or {}).get("tmux_session", "main")
    return "main"


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Seam for tests (monkeypatch to fake tmux). Real path = argv exec, no shell."""
    p = subprocess.run(argv, capture_output=True)  # noqa: S603 — argv list, no shell
    return (
        p.returncode,
        p.stdout.decode(errors="replace"),
        p.stderr.decode(errors="replace"),
    )


@router.post("/api/terminal/insert")
def insert(body: InsertBody) -> dict:
    text = body.text
    # Reject ALL control chars (not just \n\r): with `-l` tmux types bytes
    # literally, so \x03 would deliver Ctrl-C and \x1b starts escape sequences.
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise HTTPException(400, "control characters not allowed — type-only, press Enter in the pane")
    if len(text) > _MAX:
        raise HTTPException(400, f"text exceeds {_MAX} chars")
    rc, _out, err = _run(["tmux", "send-keys", "-t", _tmux_session(), "-l", "--", text])
    if rc != 0:
        return {"status": "error", "detail": err.strip()}
    return {"status": "ok"}
