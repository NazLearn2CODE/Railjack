import asyncio
import uuid
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Awaitable
from app.core.provider import ClaudeSdkProvider, Provider, ToolDecision
from app.core.scheduler import HiveMindScheduler
from app.core.security import SecurityPolicy

logger = logging.getLogger("orbiter.agent")

# Project root: app/core/agent.py → parents[2] is the Orbiter root (for B-sessions/ logs).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# PER-CALL APPROVAL via a PreToolUse hook (not the SDK's can_use_tool).
# Under the z.ai/GLM backend the CLI never invokes --permission-prompt-tool, so
# can_use_tool is dormant (0 callbacks, verified CLI 2.1.191). But PreToolUse
# hooks ride the same control protocol and DO fire (verified: hook fires, native
# Bash executes after it returns allow). So the gate lives on the hook, gating
# the NATIVE Bash/Write/Edit — no tool reimplementation, full capabilities kept.
# Read-only tools (Read/Grep/Glob/WebSearch/WebFetch) carry no hook → auto-run.
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebSearch", "WebFetch"]

# Seconds the operator has to decide on a dangerous tool call. On expiry the
# hook DENIES (fail-closed) — an idle operator never lets a blocked tool through.
APPROVAL_TIMEOUT = 600.0


class AgentSession:
    """
    Manages a single agent session/run. Streams structured events onto an
    asyncio.Queue (the event bus) consumed by the WebSocket gateway. Dangerous
    tool calls surface as `approval_needed` events resolved via approve_tool().
    """
    def __init__(self, session_id: str, prompt: str, scheduler: HiveMindScheduler, system_prompt: Optional[str] = None, security: Optional[SecurityPolicy] = None, provider: Optional[Provider] = None, allowed_tools: Optional[list[str]] = None, kind: str = "single", event_sink: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None):
        self.session_id = session_id
        self.prompt = prompt
        self.scheduler = scheduler
        self.system_prompt = system_prompt
        self.security = security
        self.provider = provider or ClaudeSdkProvider()
        # Per-session tool set. Workers (orchestrator.Team) pass a role-scoped
        # subset excluding "delegate" (leaves); the supervisor passes native + "delegate".
        self.allowed_tools = allowed_tools if allowed_tools is not None else ALLOWED_TOOLS
        # "single" (plain session) | "supervisor" (a Team's supervisor — carries delegate).
        # Workers are transient (spawned inside delegate) and never registered, so no "worker" kind surfaces.
        self.kind = kind
        # Optional forward-every-event sink. A Team supervisor sets this on each
        # worker so the worker's inner activity streams onto the supervisor's bus
        # (and replays via ingest) — see orchestrator.Team.delegate.
        self.event_sink = event_sink
        self.pending_approvals: Dict[str, asyncio.Future] = {}
        self.messages: list = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.tokens_consumed = 0
        self.status = "created"
        self.start_time = 0.0
        self.error_message: Optional[str] = None

    async def _emit(self, event: Dict[str, Any]) -> None:
        await self.events.put(event)
        if self.event_sink is not None:
            await self.event_sink(event)

    async def ingest(self, event: Dict[str, Any]) -> None:
        """Append an externally-produced event (e.g. a worker's, forwarded by
        Team.delegate) to this session's bus AND message log, so it streams live
        through /ws and replays via GET /api/sessions/{id}. Does NOT re-forward to
        this session's own sink (no recursion) — uses events.put directly.
        """
        self.messages.append(event)
        await self.events.put(event)

    async def approve_tool(self, approval_id: str, approve: bool):
        """Resolves a pending tool-execution approval from the dashboard."""
        future = self.pending_approvals.pop(approval_id, None)
        if future and not future.done():
            future.set_result(approve)
            logger.info("Session %s tool approval %s: %s", self.session_id, approval_id, approve)

    async def _on_tool_use(self, tool_name: str, tool_input: dict) -> ToolDecision:
        """OS-policy gate for dangerous tools (Bash/Write/Edit). Returns a verdict
        the provider translates into the SDK's PreToolUse hook protocol.

        Two stages:
        1. POLICY FLOOR (L1/L2) — catastrophic actions hard-deny immediately, never
           reaching the operator. Outranks approval: irrecoverable commands and
           out-of-workspace writes are blocked regardless of who clicks Approve.
        2. OPERATOR APPROVAL — the remaining (risky-but-legitimate) calls surface as
           `approval_needed` and block on the dashboard, fail-closed at APPROVAL_TIMEOUT.

        L4 mints exactly one HMAC receipt per gated call on every outcome (policy-deny,
        operator-allow, operator-deny, timeout-deny). This is the path that actually
        fires under the z.ai/GLM backend (see provider.DANGEROUS_TOOLS).
        """
        # 1. Policy floor.
        if self.security is not None:
            decision = self.security.evaluate(tool_name, tool_input, self.session_id)
            if decision.hard_deny:
                self.security.mint_and_append_receipt(self.session_id, tool_name, tool_input, "deny")
                logger.warning("Session %s policy-denied %s: %s", self.session_id, tool_name, decision.reason)
                return ToolDecision(allow=False, reason=f"Blocked by Orbiter security policy: {decision.reason}")

        # 2. Operator approval.
        approval_id = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending_approvals[approval_id] = future
        self.status = "waiting_approval"

        logger.warning("Session %s requesting approval for %s", self.session_id, tool_name)
        await self._emit({
            "type": "approval_needed",
            "approval_id": approval_id,
            "tool": tool_name,
            "input": tool_input,
        })

        try:
            # Fail-closed: deny if the operator doesn't decide in time.
            approved = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Approval %s timed out after %ss — denying", approval_id, APPROVAL_TIMEOUT)
            approved = False
        finally:
            self.pending_approvals.pop(approval_id, None)
            self.status = "running"

        # L4: one receipt for the operator's final decision.
        if self.security is not None:
            self.security.mint_and_append_receipt(self.session_id, tool_name, tool_input, "allow" if approved else "deny")

        if approved:
            return ToolDecision(allow=True)
        return ToolDecision(allow=False, reason="Execution denied by operator via dashboard (or approval timed out).")

    def final_text(self) -> str:
        """Last assistant text from the session, or '' if none.

        Used by orchestrator.Team.delegate() to return a worker's output to its
        supervisor as a tool result.
        """
        for msg in reversed(self.messages):
            if msg.get("type") != "message" or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                if texts:
                    return "\n".join(texts)
        return ""

    def save_log(self):
        """Saves a session log to B-sessions/ conforming to session-template.md."""
        date_str = datetime.date.today().isoformat()
        filename = f"{date_str}-{self.session_id[:8]}.md"
        log_dir = PROJECT_ROOT / "B-sessions"
        filepath = log_dir / filename

        content = f"""---
title: Session Log
date: {date_str}
focus: {self.prompt[:50].strip()}...
model_tier: medium
---

# {date_str} - {self.prompt}

## Goals

Run autonomous agent query: "{self.prompt}"

## What Was Done

- **Session ID:** {self.session_id}
- **Final Status:** {self.status.upper()}
- **Tokens Consumed:** {self.tokens_consumed}
- **Error:** {self.error_message or "None"}

## Conversation Log

"""
        for msg in self.messages:
            if msg.get("type") == "message":
                role = msg.get("role", "").capitalize()
                content += f"### {role}\n\n"
                blocks = msg.get("content")
                if isinstance(blocks, str):
                    content += f"{blocks}\n\n"
                elif isinstance(blocks, list):
                    for b in blocks:
                        if b.get("type") == "text":
                            content += f"{b.get('text')}\n\n"
                        elif b.get("type") == "thinking":
                            content += f"> [!NOTE]\n> **Thinking:**\n> {b.get('thinking')}\n\n"
                        elif b.get("type") == "tool_use":
                            content += f"> [!IMPORTANT]\n> **Tool Call:** `{b.get('name')}`\n> **Input:**\n> ```json\n> {b.get('input')}\n> ```\n\n"
            elif msg.get("type") == "result":
                content += f"> [!TIP]\n> **Tool Result (Error: {msg.get('is_error')}):**\n> ```\n> {msg.get('result')}\n> ```\n\n"

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content, encoding="utf-8")
            logger.info("Saved session log to %s", filepath)
        except Exception as e:
            logger.error("Failed to save session log: %s", e)

    async def run(self) -> None:
        """
        Runs the agent loop under scheduler control, streaming normalized provider
        events onto self.events. Emits a terminal `stream_end` when done. The
        provider keeps the underlying LLM in streaming/control-protocol mode, which
        is what lets the PreToolUse approval gate fire.
        """
        self.status = "pending_admission"
        await self._emit({"type": "status", "status": self.status})

        try:
            self.start_time = await self.scheduler.enter_turn(self.session_id)
            self.status = "running"
            await self._emit({"type": "status", "status": self.status})

            success = True
            over_budget = False
            async for event in self.provider.stream(
                self.prompt,
                system_prompt=self.system_prompt,
                allowed_tools=self.allowed_tools,
                session_id=self.session_id,
                on_tool_use=self._on_tool_use,
            ):
                self.messages.append(event)
                await self._emit(event)

                if event.get("type") == "message" and event.get("role") == "assistant" and event.get("usage"):
                    # Both display and budget count full throughput (input + cache + output);
                    # see _throughput(). The budget ceiling is tuned for this — scheduler.py.
                    used = self._throughput(event["usage"])
                    self.tokens_consumed += used
                    # Mid-turn budget enforcement: stop the moment a session crosses its ceiling.
                    if not self.scheduler.token_budget.consume(self.session_id, used):
                        self.error_message = f"Token budget exceeded ({self.scheduler.token_budget.default_ceiling})."
                        over_budget = True
                        break

                if event.get("type") == "result":
                    if event.get("is_error"):
                        success = False
                        self.error_message = str(event.get("result"))
                    if event.get("usage"):
                        self.tokens_consumed = self._throughput(event["usage"])

            await self.scheduler.exit_turn(self.session_id, self.start_time, success, actual_tokens=self.tokens_consumed)
            self.status = "failed" if over_budget else "completed"
            await self._emit(
                {"type": "status", "status": self.status, **({"error": self.error_message} if over_budget else {})}
            )

        except Exception as e:
            logger.error("Error executing agent session %s: %s", self.session_id, e, exc_info=True)
            self.status = "failed"
            self.error_message = str(e)
            if self.start_time > 0.0:
                await self.scheduler.exit_turn(self.session_id, self.start_time, False, actual_tokens=0)
            await self._emit({"type": "status", "status": self.status, "error": self.error_message})
        finally:
            await self._emit({"type": "stream_end"})
            self.save_log()

    @staticmethod
    def _throughput(usage: Any) -> int:
        """Full token throughput: input + cache read/write + output (observability).

        cache_read_input_tokens is ~99% of volume once the system prompt + tool defs
        are cached, so input+output alone undercounts by 100x+. The token *budget*
        bounds input+output only — see run().
        """
        if not usage:
            return 0
        return (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("output_tokens", 0)
        )

class AgentSessionManager:
    """Orchestrates all active and historical AgentSession instances."""
    def __init__(self, scheduler: HiveMindScheduler, security: Optional[SecurityPolicy] = None, provider: Optional[Provider] = None):
        self.scheduler = scheduler
        self.security = security
        self.provider = provider
        self.sessions: Dict[str, AgentSession] = {}

    def create_session(self, prompt: str, system_prompt: Optional[str] = None) -> AgentSession:
        session_id = str(uuid.uuid4())
        session = AgentSession(session_id, prompt, self.scheduler, system_prompt,
                               security=self.security, provider=self.provider)
        self.sessions[session_id] = session
        return session

    def register(self, session: AgentSession) -> AgentSession:
        """Adopt an externally-built session (e.g. a Team supervisor) so the shared
        WS / approve / detail surface drives it like any other."""
        self.sessions[session.session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        return self.sessions.get(session_id)

    def list_sessions(self) -> list:
        return [
            {
                "session_id": s.session_id,
                "prompt": s.prompt,
                "status": s.status,
                "kind": s.kind,
                "tokens_consumed": s.tokens_consumed,
                "error": s.error_message,
            }
            for s in self.sessions.values()
        ]
