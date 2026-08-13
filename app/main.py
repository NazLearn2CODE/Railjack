"""Railjack hub — FastAPI app.

``GET /api/config`` (machine name + sanitized module list), ``/api/health`` and
``/api/modules/...`` (manage), then a static mount of ``frontend/dist`` last so /api
wins.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .catalog import router as catalog_router
from .comfyui import router as comfyui_router
from .config import CONFIG, Module, reload_config
from .ffmpeg_jobs import router as ffmpeg_router
from .health import router as health_router
from .kanban import router as kanban_router
from .manage import router as manage_router
from .newsroom import router as newsroom_router
from .notebooklm import router as notebooklm_router
from .radio_news import router as radio_news_router
from .n8n_proxy import router as n8n_proxy_router
from .session_stats import router as session_router
from .terminal_input import router as terminal_router
from .thailandnow import router as thailandnow_router
from .fireside import router as fireside_router

app = FastAPI(title="Railjack")
app.include_router(health_router)
app.include_router(manage_router)
app.include_router(ffmpeg_router)
app.include_router(comfyui_router)
app.include_router(notebooklm_router)
app.include_router(newsroom_router)
app.include_router(radio_news_router)
app.include_router(catalog_router)
app.include_router(session_router)
app.include_router(terminal_router)
app.include_router(thailandnow_router)
app.include_router(kanban_router)
app.include_router(fireside_router)
# Phase E: same-origin reverse proxy for the n8n editor (mounted before the
# catch-all static mount below so /n8n/* wins over frontend/dist).
app.include_router(n8n_proxy_router)

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
    # Per-module opt-in for the LIVE terminal dock (default False). The frontend
    # only renders the dock when the ACTIVE module has this true.
    out["live_dock"] = m.live_dock
    # start_timeout_s is safe to expose (no argv/units): the UI keeps a module's
    # pip amber "STARTING…" for this long after a fire-and-forget (pending) start.
    if m.manage is not None:
        out["start_timeout_s"] = m.manage.start_timeout_s
    return out


def _serialize_config() -> dict:
    """Frontend-safe projection of the live CONFIG (shared by GET and reload)."""
    cfg = {
        "machine": CONFIG.machine,
        "modules": [_sanitize(m) for m in CONFIG.modules],
        # Cockpit buttons: label/insert text only (Naz-editable YAML prompts).
        "buttons": [{"label": b.label, "insert": b.insert} for b in CONFIG.buttons],
    }
    if CONFIG.dock is not None:
        # Title/url/height only — no internals to leak. Omitted entirely when absent
        # so the frontend treats a missing `dock` as "no dock".
        cfg["dock"] = {
            "title": CONFIG.dock.title,
            "url": CONFIG.dock.url,
            "height": CONFIG.dock.height,
        }
    return cfg


@app.get("/api/config")
def get_config() -> dict:
    return _serialize_config()


@app.post("/api/config/reload")
def post_config_reload() -> dict:
    """Re-read this machine's YAML without a server restart, then return the fresh
    config so the cockpit (buttons/modules) updates live. A broken YAML leaves the
    running config untouched and returns a 400 with the parse error."""
    try:
        reload_config()
    except Exception as e:  # invalid/missing YAML — keep the old config, report why
        raise HTTPException(status_code=400, detail=f"Config reload failed: {e}") from e
    return _serialize_config()


@app.post("/api/system/restart")
async def post_system_restart() -> dict:
    """Restart the railjack service itself — load new backend code without a
    terminal. Spawns a DETACHED ``sleep 1 && systemctl --user restart railjack``
    (``start_new_session=True``) so the child survives this worker being killed;
    the 1 s delay lets this response flush first. The cockpit polls /api/health
    to see the service come back."""
    import subprocess
    subprocess.Popen(
        ["bash", "-c", "sleep 1 && systemctl --user restart railjack"],
        start_new_session=True,
    )
    return {"restarting": True}


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
