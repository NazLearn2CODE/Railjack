"""GET /api/health — parallel best-effort probe of each module's health URL.

For every module with a ``health`` spec, GET its URL with a 3 s timeout
(following redirects). 2xx/3xx → ``"ok"``; any timeout, connection error, or
other status → ``"down"``. Modules with no health spec are omitted (the UI
treats a missing key as grey/unknown). One probe failing never blocks the
others (``gather(..., return_exceptions=True)`` + per-probe try/except).
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter

from .config import CONFIG, Module

router = APIRouter()


async def _one(client: httpx.AsyncClient, module: Module) -> str:
    assert module.health is not None and module.health.url is not None
    try:
        r = await client.get(module.health.url, follow_redirects=True)
        return "ok" if r.status_code < 400 else "down"
    except (httpx.HTTPError, OSError):
        return "down"


@router.get("/api/health")
async def health() -> dict[str, str]:
    targets = [m for m in CONFIG.modules if m.health and m.health.url]
    if not targets:
        return {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        results = await asyncio.gather(
            *(_one(client, m) for m in targets), return_exceptions=True
        )
    return {
        m.id: ("down" if isinstance(r, Exception) else r)
        for m, r in zip(targets, results, strict=True)
    }
