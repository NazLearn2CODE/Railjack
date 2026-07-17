"""Railjack hub — FastAPI app.

M1 surface: ``GET /api/config`` (machine name + sanitized module list) and a
static mount of ``web/dist`` (last, so /api wins). Health/manage come in M2+.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import CONFIG, Module

app = FastAPI(title="Railjack")

DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def _sanitize(m: Module) -> dict:
    """Frontend-safe projection: keep the embed surface, drop manage/health
    internals (argv, unit names, internal probe URLs)."""
    out: dict = {"id": m.id, "title": m.title, "kind": m.kind}
    if m.url:
        out["url"] = m.url
    if m.panel:
        out["panel"] = m.panel
    return out


@app.get("/api/config")
def get_config() -> dict:
    return {"machine": CONFIG.machine, "modules": [_sanitize(m) for m in CONFIG.modules]}


# Serve the built React dashboard when present; otherwise a placeholder.
# Mounted LAST so /api/* routes above take precedence.
if DASHBOARD_DIST.is_dir():
    app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
else:

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (
            "<h1>Railjack</h1>"
            "<p>Hub up. Dashboard not built — run <code>npm install &amp;&amp; npm run build</code> "
            "in <code>web/</code>.</p>"
        )
