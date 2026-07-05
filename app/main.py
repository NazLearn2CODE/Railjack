"""Orbiter FastAPI gateway — REST + WebSocket surface over the OS core.

One process = one HiveMindScheduler + one AgentSessionManager (local single-user OS).
Dashboard mounts at "/" once the React build exists; until then a placeholder is served.
"""
import os
import json
import asyncio
import logging
from pathlib import Path
from contextlib import suppress

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core import registry
from app.core.agent import AgentSessionManager
from app.core.cephalon import probe
from app.core.orchestrator import DEFAULT_ROLES, Team, WorkerRole, default_supervisor_prompt
from app.core.provider import ClaudeSdkProvider
from app.core.scheduler import HiveMindScheduler
from app.core.sandbox import LandlockSandbox, NoopSandbox
from app.core.security import SecurityPolicy, WorkspaceBoundary, ShellPolicy, ToolReceiptLedger
from app.core.skills import scan_skills

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("orbiter")

# Project root: app/main.py → parents[1] is the Orbiter root (workspace default + receipt log).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(os.environ.get("ORBITER_WORKSPACE_ROOT", PROJECT_ROOT)).resolve(strict=False)

# Singletons — created once at import; reused across requests/connections.
scheduler = HiveMindScheduler()

# Security floor (blueprint §2.2 L1/L2/L4): workspace boundary, catastrophic-shell
# blocklist, and an HMAC-signed append-only receipt log. Env-configurable; defaults
# scope the agent to the project root and log receipts under logs/receipts.jsonl.
security = SecurityPolicy(
    boundary=WorkspaceBoundary(
        roots=[WORKSPACE_ROOT]
    ),
    shell=ShellPolicy(),
    ledger=ToolReceiptLedger(
        secret=os.environ.get("ORBITER_RECEIPT_SECRET", ""),
        log_path=Path(os.environ.get("ORBITER_RECEIPT_LOG", PROJECT_ROOT / "logs" / "receipts.jsonl")),
    ),
)

def _load_json_env(name: str, expected: type, empty):
    """Parse env var `name` as JSON of `expected` container type.

    Returns `empty` on unset / malformed / wrong-type (logged) so the gateway
    still boots. Callers do their own per-item validation.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return empty
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("%s is not valid JSON (%s); ignoring", name, e)
        return empty
    if not isinstance(parsed, expected):
        logger.error("%s must be a JSON %s; got %s", name, expected.__name__, type(parsed).__name__)
        return empty
    return parsed


def _load_mcp_servers() -> dict[str, dict]:
    """Parse ORBITER_MCP_SERVERS (JSON: {name: McpServerConfig}).

    External MCP servers are operator-configured tool sources (blueprint §3.2). A
    malformed value logs an error and yields {} so the gateway still boots — verify
    what loaded via GET /api/health (which reports names + types, never secrets).
    """
    parsed = _load_json_env("ORBITER_MCP_SERVERS", dict, {})
    # Each value must be a server-spec dict; the SDK validates the full shape at run
    # time, but a non-dict here would crash /api/health's spec.get("type"). Drop + log.
    valid = {k: v for k, v in parsed.items() if isinstance(v, dict)}
    if len(valid) != len(parsed):
        logger.error("ORBITER_MCP_SERVERS: dropped %d non-object spec(s)", len(parsed) - len(valid))
    return valid


def _load_services() -> list[dict]:
    """Parse ORBITER_SERVICES (JSON: [{"name", "url", "embed"?}]).

    Services are launcher tiles displayed in the sidebar. A malformed value logs
    an error and yields [] so the gateway still boots.
    """
    valid = []
    for item in _load_json_env("ORBITER_SERVICES", list, []):
        if isinstance(item, dict) and "name" in item and "url" in item:
            valid.append({
                "name": str(item["name"]),
                "url": str(item["url"]),
                "embed": bool(item.get("embed", False))
            })
        else:
            logger.error("ORBITER_SERVICES: skipped invalid item %s", item)
    return valid


# External MCP servers applied to every session (single-agent + supervisor + workers).
# ponytail: global, not per-session — local single-user OS, one config. Per-session
# config when an operator needs different MCP sets per run.
EXTERNAL_MCP = _load_mcp_servers()
SERVICES = _load_services()
provider = ClaudeSdkProvider(mcp_servers=EXTERNAL_MCP)
manager = AgentSessionManager(scheduler, security=security, provider=provider)

# L3 OS sandbox (blueprint §2.2): self-Landlock write-confinement at startup.
# Fail-open: NoopSandbox when disabled OR if Landlock is unavailable on this host.
def _build_sandbox():
    if os.environ.get("ORBITER_SANDBOX", "landlock").lower() == "none":
        return NoopSandbox()
    return LandlockSandbox(
        writable_roots=[
            Path(os.environ.get("ORBITER_WORKSPACE_ROOT", PROJECT_ROOT)).resolve(strict=False),
            Path(os.environ.get("TMPDIR", "/tmp")),
            Path("~/.claude").expanduser(),
        ],
        extra_roots=os.environ.get("ORBITER_SANDBOX_EXTRA_ROOTS"),
    )


sandbox = _build_sandbox()
sandbox_status = sandbox.apply()

app = FastAPI(title="Orbiter", version="0.1.0")


class CreateSession(BaseModel):
    prompt: str
    system_prompt: str | None = None
    provider: str | None = None
    model: str | None = None


class RegisterProvider(BaseModel):
    name: str
    base_url: str | None = None
    auth_env: str | None = None
    api_key: str | None = None
    models: list[str] | None = None


class ApproveTool(BaseModel):
    approval_id: str
    approve: bool


class RoleSpec(BaseModel):
    name: str
    system_prompt: str


class CreateTeam(BaseModel):
    prompt: str
    system_prompt: str | None = None
    roles: list[RoleSpec] | None = None
    provider: str | None = None
    model: str | None = None


@app.post("/api/teams")
async def create_team(req: CreateTeam):
    """Spawn a Centralized-topology team and register its supervisor.

    The supervisor is an ordinary AgentSession (carrying the `delegate` tool), so
    the existing /ws/sessions/{id} stream + /approve gate + GET detail drive it
    like any single-agent run — delegation surfaces as `delegate` tool calls.
    """
    try:
        worker_provider = ClaudeSdkProvider(mcp_servers=EXTERNAL_MCP)
        if req.provider:
            resolved_model, env_overrides = registry.resolve(req.provider, req.model)
            worker_provider = ClaudeSdkProvider(
                mcp_servers=EXTERNAL_MCP,
                model=resolved_model,
                env=env_overrides,
            )
        team = Team(
            scheduler,
            security=security,
            worker_provider=worker_provider,
            register=manager.register,
        )
        if req.roles:
            team.hire(*(WorkerRole(name=r.name, system_prompt=r.system_prompt) for r in req.roles))
        else:
            team.hire(*DEFAULT_ROLES)
        roles = list(team.roles)

        sup = team.supervisor(
            req.prompt,
            system_prompt=req.system_prompt or default_supervisor_prompt(list(team.roles.values())),
        )
        manager.register(sup)
        return {"session_id": sup.session_id, "status": sup.status, "prompt": sup.prompt, "kind": sup.kind, "roles": roles}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/sessions")
async def create_session(req: CreateSession):
    try:
        custom_provider = None
        if req.provider:
            resolved_model, env_overrides = registry.resolve(req.provider, req.model)
            custom_provider = ClaudeSdkProvider(
                mcp_servers=EXTERNAL_MCP,
                model=resolved_model,
                env=env_overrides,
            )
        s = manager.create_session(req.prompt, req.system_prompt, provider=custom_provider)
        return {"session_id": s.session_id, "status": s.status, "prompt": s.prompt}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/providers")
async def list_providers():
    return registry.public_view()


@app.post("/api/providers")
async def register_provider(req: RegisterProvider):
    registry.add_provider(
        name=req.name,
        base_url=req.base_url,
        auth_env=req.auth_env,
        api_key=req.api_key,
        models=req.models,
    )
    return {"ok": True}


@app.get("/api/fs/list")
async def fs_list(path: str = "."):
    try:
        p = Path(path).resolve()
        if not p.is_dir():
            p = Path(".").resolve()
        
        subdirs = []
        if p.parent != p:
            subdirs.append({"name": "..", "path": str(p.parent.resolve())})
            
        for child in p.iterdir():
            try:
                if child.is_dir() and not child.name.startswith("."):
                    subdirs.append({"name": child.name, "path": str(child.resolve())})
            except PermissionError:
                continue
                
        subdirs.sort(key=lambda x: (x["name"] != "..", x["name"].lower()))
        return {"current": str(p.resolve()), "dirs": subdirs}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))





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
        "kind": s.kind,
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


@app.get("/api/skills")
async def list_skills():
    return scan_skills(WORKSPACE_ROOT)


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


@app.post("/api/workspace-root")
async def set_workspace_root(req: dict):
    path = req.get("root", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="root path required")
    p = Path(path).resolve()
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"not a directory: {path}")
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = p
    # ponytail: re-scope security boundary to new root
    security.boundary = WorkspaceBoundary(roots=[WORKSPACE_ROOT])
    from app.core.cephalon import probe
    probe.cache_clear()
    return {"root": str(WORKSPACE_ROOT)}


@app.get("/api/health")
async def health():
    return {
        "status": "OK",
        "sandbox": {
            "active": sandbox_status.active,
            "mechanism": sandbox_status.mechanism,
            "abi": sandbox_status.abi,
            "writable_roots": sandbox_status.writable_roots,
            "reason": sandbox_status.reason,
        },
        # Names + transport only — never env/headers (may carry secrets).
        "mcp_servers": [
            {"name": name, "type": spec.get("type") or "stdio"}
            for name, spec in EXTERNAL_MCP.items()
        ],
        "services": SERVICES,
        "workspace": probe(WORKSPACE_ROOT),
    }


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
