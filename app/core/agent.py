import asyncio
import uuid
import logging
import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, ResultMessage, UserMessage, RateLimitEvent, HookMatcher
from app.core.scheduler import HiveMindScheduler

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
DANGEROUS_TOOLS = "Bash|Write|Edit"
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
    def __init__(self, session_id: str, prompt: str, scheduler: HiveMindScheduler, system_prompt: Optional[str] = None):
        self.session_id = session_id
        self.prompt = prompt
        self.scheduler = scheduler
        self.system_prompt = system_prompt
        self.pending_approvals: Dict[str, asyncio.Future] = {}
        self.messages: list = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.tokens_consumed = 0
        self.status = "created"
        self.start_time = 0.0
        self.error_message: Optional[str] = None

    async def _emit(self, event: Dict[str, Any]) -> None:
        await self.events.put(event)

    async def _prompt_stream(self):
        """Yields the user prompt as a stream-json message (streaming mode)."""
        yield {
            "type": "user",
            "message": {"role": "user", "content": self.prompt},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    async def approve_tool(self, approval_id: str, approve: bool):
        """Resolves a pending tool-execution approval from the dashboard."""
        future = self.pending_approvals.pop(approval_id, None)
        if future and not future.done():
            future.set_result(approve)
            logger.info("Session %s tool approval %s: %s", self.session_id, approval_id, approve)

    async def _pre_tool_use_hook(self, hook_input: dict, tool_use_id: Optional[str], context) -> dict:
        """PreToolUse gate for dangerous tools (Bash/Write/Edit).

        Emits an `approval_needed` event and blocks on operator approval from the
        dashboard (resolved via approve_tool()). Returns allow/deny to the CLI.
        This is the path that actually fires under the z.ai/GLM backend — see the
        DANGEROUS_TOOLS comment above for why the hook and not can_use_tool.
        """
        tool_name = hook_input.get("tool_name", "?")
        tool_input = hook_input.get("tool_input", {})

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

        if approved:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Execution denied by operator via dashboard (or approval timed out).",
            }
        }

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
        Runs the agent loop under scheduler control via the streaming query() API,
        pushing structured events onto self.events. Emits a terminal `stream_end`
        when done. The AsyncIterable prompt keeps the SDK in streaming/control-
        protocol mode, which is what lets the PreToolUse approval hook fire.
        """
        self.status = "pending_admission"
        await self._emit({"type": "status", "status": self.status})

        try:
            self.start_time = await self.scheduler.enter_turn(self.session_id)
            self.status = "running"
            await self._emit({"type": "status", "status": self.status})

            options = ClaudeAgentOptions(
                system_prompt=self.system_prompt,
                allowed_tools=ALLOWED_TOOLS,
                hooks={
                    "PreToolUse": [
                        HookMatcher(
                            matcher=DANGEROUS_TOOLS,
                            hooks=[self._pre_tool_use_hook],
                            timeout=APPROVAL_TIMEOUT + 10,  # CLI waits slightly longer than our fail-closed deadline
                        )
                    ]
                },
                setting_sources=[],
                session_id=self.session_id,
            )

            success = True
            over_budget = False
            async for message in query(prompt=self._prompt_stream(), options=options):
                msg_data = self._serialize_message(message)
                if msg_data:
                    self.messages.append(msg_data)
                    await self._emit(msg_data)

                if isinstance(message, AssistantMessage) and message.usage:
                    turn_tokens = message.usage.get("input_tokens", 0) + message.usage.get("output_tokens", 0)
                    self.tokens_consumed += turn_tokens
                    # Mid-turn budget enforcement: stop the moment a session crosses its ceiling.
                    if not self.scheduler.token_budget.consume(self.session_id, turn_tokens):
                        self.error_message = f"Token budget exceeded ({self.scheduler.token_budget.default_ceiling})."
                        over_budget = True
                        break

                if isinstance(message, ResultMessage):
                    if message.is_error:
                        success = False
                        self.error_message = str(message.result)
                    if message.usage:
                        self.tokens_consumed = message.usage.get("input_tokens", 0) + message.usage.get("output_tokens", 0)

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
    def _safe(obj: Any) -> Any:
        """Recursively coerce SDK block objects into JSON-serializable structures."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, list):
            return [AgentSession._safe(x) for x in obj]
        if isinstance(obj, dict):
            return {k: AgentSession._safe(v) for k, v in obj.items()}
        if hasattr(obj, "model_dump"):  # pydantic
            return obj.model_dump()
        if hasattr(obj, "__dataclass_fields__"):  # dataclass (ToolResultBlock, etc.)
            return {k: AgentSession._safe(getattr(obj, k)) for k in obj.__dataclass_fields__}
        return str(obj)

    def _serialize_message(self, message: Any) -> Optional[Dict[str, Any]]:
        """Serializes Claude SDK Message types to JSON-friendly dicts."""
        if isinstance(message, UserMessage):
            # content may be a string or a list of block objects (e.g. ToolResultBlock)
            return {"type": "message", "role": "user", "content": self._safe(message.content), "uuid": message.uuid}
        elif isinstance(message, AssistantMessage):
            content_blocks = []
            for block in message.content:
                if hasattr(block, "text"):
                    content_blocks.append({"type": "text", "text": block.text})
                elif hasattr(block, "thinking"):
                    content_blocks.append({"type": "thinking", "thinking": block.thinking})
                elif hasattr(block, "name") and hasattr(block, "id"):
                    content_blocks.append({
                        "type": "tool_use",
                        "tool_use_id": block.id,
                        "name": block.name,
                        "input": getattr(block, "input", {}),
                    })
            return {
                "type": "message",
                "role": "assistant",
                "content": content_blocks,
                "uuid": message.uuid,
                "usage": message.usage,
            }
        elif isinstance(message, ResultMessage):
            return {"type": "result", "result": message.result, "is_error": message.is_error, "usage": message.usage, "uuid": message.uuid}
        elif isinstance(message, RateLimitEvent):
            return {"type": "rate_limit", "rate_limit_type": message.rate_limit_type, "info": getattr(message, "info", None)}
        return None


class AgentSessionManager:
    """Orchestrates all active and historical AgentSession instances."""
    def __init__(self, scheduler: HiveMindScheduler):
        self.scheduler = scheduler
        self.sessions: Dict[str, AgentSession] = {}

    def create_session(self, prompt: str, system_prompt: Optional[str] = None) -> AgentSession:
        session_id = str(uuid.uuid4())
        session = AgentSession(session_id, prompt, self.scheduler, system_prompt)
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        return self.sessions.get(session_id)

    def list_sessions(self) -> list:
        return [
            {
                "session_id": s.session_id,
                "prompt": s.prompt,
                "status": s.status,
                "tokens_consumed": s.tokens_consumed,
                "error": s.error_message,
            }
            for s in self.sessions.values()
        ]
