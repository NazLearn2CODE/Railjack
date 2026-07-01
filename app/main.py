"""Orbiter FastAPI gateway — REST + WebSocket surface over the OS core.

One process = one HiveMindScheduler + one AgentSessionManager (local single-user OS).
Dashboard mounts at "/" once the React build exists; until then a placeholder is served.
"""
import os
import asyncio
import logging
from pathlib import Path
from contextlib import suppress

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core.agent import AgentSessionManager
from app.core.scheduler import HiveMindScheduler
from app.core.security import SecurityPolicy, WorkspaceBoundary, ShellPolicy, ToolReceiptLedger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("orbiter")

# Project root: app/main.py → parents[1] is the Orbiter root (workspace default + receipt log).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Singletons — created once at import; reused across requests/connections.
scheduler = HiveMindScheduler()

# Security floor (blueprint §2.2 L1/L2/L4): workspace boundary, catastrophic-shell
# blocklist, and an HMAC-signed append-only receipt log. Env-configurable; defaults
# scope the agent to the project root and log receipts under logs/receipts.jsonl.
security = SecurityPolicy(
    boundary=WorkspaceBoundary(
        roots=[Path(os.environ.get("ORBITER_WORKSPACE_ROOT", PROJECT_ROOT)).resolve(strict=False)]
    ),
    shell=ShellPolicy(),
    ledger=ToolReceiptLedger(
        secret=os.environ.get("ORBITER_RECEIPT_SECRET", ""),
        log_path=Path(os.environ.get("ORBITER_RECEIPT_LOG", PROJECT_ROOT / "logs" / "receipts.jsonl")),
    ),
)

manager = AgentSessionManager(scheduler, security=security)

app = FastAPI(title="Orbiter", version="0.1.0")


class CreateSession(BaseModel):
    prompt: str
    system_prompt: str | None = None


class ApproveTool(BaseModel):
    approval_id: str
    approve: bool


@app.post("/api/sessions")
async def create_session(req: CreateSession):
    s = manager.create_session(req.prompt, req.system_prompt)
    return {"session_id": s.session_id, "status": s.status, "prompt": s.prompt}


@app.get("/api/sessions")
async def list_sessions():
    return manager.list_sessions()


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    s = manager.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": s.session_id,
        "prompt": s.prompt,
        "status": s.status,
        "tokens_consumed": s.tokens_consumed,
        "error": s.error_message,
        "messages": s.messages,
    }


@app.post("/api/sessions/{session_id}/approve")
async def approve_tool(session_id: str, req: ApproveTool):
    # Resolves a pending tool approval emitted by the agent's PreToolUse hook.
    s = manager.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    await s.approve_tool(req.approval_id, req.approve)
    return {"ok": True}


@app.websocket("/ws/sessions/{session_id}")
async def stream_session(ws: WebSocket, session_id: str):
    """Drive one agent session; events flow from its asyncio.Queue event bus.

    Dangerous tool calls surface as `approval_needed`; the operator resolves them
    via POST /api/sessions/{id}/approve. Disconnect cancels the run (its finally
    block disconnects the SDK client).
    """
    s = manager.get_session(session_id)
    if s is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    task = asyncio.create_task(s.run())
    try:
        while True:
            event = await s.events.get()
            await ws.send_json(event)
            if event.get("type") == "stream_end":
                break
    except WebSocketDisconnect:
        logger.info("client disconnected from %s", session_id)
    except Exception as e:  # noqa: BLE001 — surface any failure to the client, then drop
        logger.exception("stream error on %s", session_id)
        with suppress(Exception):
            await ws.send_json({"type": "status", "status": "failed", "error": str(e)})
    finally:
        if not task.done():
            task.cancel()
            with suppress(Exception):
                await task


DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@app.get("/api/health")
async def health():
    return {"status": "OK"}


# Serve the built React dashboard when present; otherwise a placeholder.
# Mounted last so /api/* and /ws/* routes (registered above) take precedence.
if DASHBOARD_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
else:

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (
            "<h1>Orbiter</h1>"
            "<p>Gateway up. Dashboard not built — run <code>npm install &amp;&amp; npm run build</code> "
            "in <code>web/</code>.</p>"
        )
