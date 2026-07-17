"""POST /api/modules/{id}/action + GET /api/modules/{id}/logs.

``systemd-user`` modules → ``systemctl --user <action> <unit>`` (then the same
for each ``extra_units``). ``command`` modules → ``spec.start``/``spec.stop``/
``spec.log`` argv. All subprocesses via ``asyncio.create_subprocess_exec`` —
never ``shell=True``, never sudo. Slow command starts are fire-and-forget
(``{"status": "pending"}``); the amber STARTING… polling UX lands in M3.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import CONFIG, Module

router = APIRouter()

_LOG_MAX = 500
_ACTIONS = {"start", "stop", "restart"}

# Hold refs to fire-and-forget tasks so CPython doesn't GC them mid-run.
_BG: set[asyncio.Task[object]] = set()


class ActionBody(BaseModel):
    action: str  # start | stop | restart


def _find(module_id: str) -> Module:
    for m in CONFIG.modules:
        if m.id == module_id:
            return m
    raise HTTPException(404, "unknown module")


async def _run(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out_b, err_b = await proc.communicate()
    return proc.returncode or 0, out_b.decode(errors="replace"), err_b.decode(errors="replace")


def _spawn(argv: list[str]) -> None:
    """Fire-and-forget a subprocess; result is intentionally discarded."""
    task = asyncio.create_task(_run(argv))
    _BG.add(task)
    task.add_done_callback(_BG.discard)


@router.post("/api/modules/{module_id}/action")
async def action(module_id: str, body: ActionBody) -> dict:
    m = _find(module_id)
    if m.manage is None:
        raise HTTPException(400, "module not manageable")
    if body.action not in _ACTIONS:
        raise HTTPException(400, "unknown action")
    mg = m.manage

    if mg.type == "systemd-user":
        if not mg.unit:
            raise HTTPException(400, "systemd-user module has no unit")
        for unit in [mg.unit, *mg.extra_units]:
            rc, _out, err = await _run(["systemctl", "--user", body.action, unit])
            if rc != 0:
                return {"status": "error", "detail": err.strip()}
        return {"status": "ok", "detail": ""}

    if mg.type == "command":
        if body.action == "stop":
            if not mg.stop:
                raise HTTPException(400, "command module has no stop argv")
            rc, _out, err = await _run(mg.stop)
            return {"status": "ok" if rc == 0 else "error", "detail": err.strip()}
        # start, or restart (= stop-then-start): stop awaited, start fire-and-forget.
        if body.action == "restart" and mg.stop:
            rc, _out, err = await _run(mg.stop)
            if rc != 0:
                return {"status": "error", "detail": err.strip()}
        if not mg.start:
            raise HTTPException(400, "command module has no start argv")
        _spawn(mg.start)
        return {"status": "pending", "detail": ""}

    raise HTTPException(400, f"unknown manage type {mg.type!r}")


@router.get("/api/modules/{module_id}/logs")
async def logs(module_id: str, n: int = 50) -> dict:
    m = _find(module_id)
    if m.manage is None:
        raise HTTPException(400, "module not manageable")
    n = max(1, min(n, _LOG_MAX))
    mg = m.manage

    if mg.type == "systemd-user":
        if not mg.unit:
            raise HTTPException(400, "systemd-user module has no unit")
        argv = ["journalctl", "--user", "-u", mg.unit, "-n", str(n), "--no-pager"]
    elif mg.type == "command":
        if not mg.log:
            raise HTTPException(400, "command module has no log argv")
        argv = mg.log
    else:
        raise HTTPException(400, f"unknown manage type {mg.type!r}")

    _rc, out, err = await _run(argv)
    return {"lines": out or err}
