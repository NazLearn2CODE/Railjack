"""Railjack hub — FastAPI app.

``GET /api/config`` (machine name + sanitized module list), ``/api/health`` and
``/api/modules/...`` (manage), then a static mount of ``frontend/dist`` last so /api
wins.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .catalog import router as catalog_router
from .comfyui import router as comfyui_router
from .config import CONFIG, Module
from .ffmpeg_jobs import router as ffmpeg_router
from .health import router as health_router
from .manage import router as manage_router
from .session_stats import router as session_router
from .terminal_input import router as terminal_router

app = FastAPI(title="Railjack")
app.include_router(health_router)
app.include_router(manage_router)
app.include_router(ffmpeg_router)
app.include_router(comfyui_router)
app.include_router(catalog_router)
app.include_router(session_router)
app.include_router(terminal_router)

DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _sanitize(m: Module) -> dict:
    """Frontend-safe projection: keep the embed surface and bare health/manage
    presence flags, but drop internals (argv, unit names, probe URLs)."""
    out: dict = {"id": m.id, "title": m.title, "kind": m.kind}
    if m.url:
        out["url"] = m.url
    if m.panel:
        out["panel"] = m.panel
    out["health"] = m.health is not None
    out["manage"] = m.manage is not None
    # start_timeout_s is safe to expose (no argv/units): the UI keeps a module's
    # pip amber "STARTING…" for this long after a fire-and-forget (pending) start.
    if m.manage is not None:
        out["start_timeout_s"] = m.manage.start_timeout_s
    return out


@app.get("/api/config")
def get_config() -> dict:
    return {
        "machine": CONFIG.machine,
        "modules": [_sanitize(m) for m in CONFIG.modules],
        # Cockpit buttons: label/insert text only (Naz-editable YAML prompts).
        "buttons": [{"label": b.label, "insert": b.insert} for b in CONFIG.buttons],
    }


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
            "in <code>frontend/</code>.</p>"
        )
