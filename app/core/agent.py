import asyncio
import uuid
import logging
import datetime
import os
from typing import Dict, Any, Optional
from claude_agent_sdk import query, ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny
from claude_agent_sdk.types import AssistantMessage, ResultMessage, UserMessage, SystemMessage, StreamEvent, RateLimitEvent
from app.core.scheduler import HiveMindScheduler

logger = logging.getLogger("orbiter.agent")

# Read-only subset, referenced by the (currently dormant) approval callback.
SAFE_TOOLS = {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}

# LOCAL AUTONOMY: every tool auto-allowed. can_use_tool is wired but dormant —
# under the z.ai/GLM backend the CLI never invokes --permission-prompt-tool
# (verified 0 callback calls across CLI 2.1.139/2.1.191, modes default/plan,
# setting_sources []/default). Per-call approval is deferred. To restore it:
# switch to the native Anthropic API, or expose bash/edit/write as custom SDK
# MCP tools (in-process, so ungated-bypass-proof).
ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebSearch", "WebFetch"]


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

    async def _can_use_tool_callback(self, tool_name: str, tool_input: dict, context) -> Any:
        """SDK permission callback. Safe tools pass; others block on operator approval."""
        if tool_name in SAFE_TOOLS:
            return PermissionResultAllow()

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

        approved = await future
        self.status = "running"
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Execution denied by operator via dashboard.")

    def save_log(self):
        """Saves a session log to B-sessions/ conforming to session-template.md."""
        # ponytail: hardcoded path resolves via /home -> /var/home symlink; derive from __file__ later (task #6).
        date_str = datetime.date.today().isoformat()
        filename = f"{date_str}-{self.session_id[:8]}.md"
        filepath = f"/home/NAZ/Coding Projects/Orbiter/B-sessions/{filename}"

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
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Saved session log to %s", filepath)
        except Exception as e:
            logger.error("Failed to save session log: %s", e)

    async def run(self) -> None:
        """
        Runs the agent loop under scheduler control via the streaming query() API,
        pushing structured events onto self.events. Emits a terminal `stream_end`
        when done. The AsyncIterable prompt is required so can_use_tool's control
        protocol (permission_prompt_tool stdio) is enabled.
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
                can_use_tool=self._can_use_tool_callback,
                setting_sources=[],
                session_id=self.session_id,
            )

            success = True
            async for message in query(prompt=self._prompt_stream(), options=options):
                msg_data = self._serialize_message(message)
                if msg_data:
                    self.messages.append(msg_data)
                    await self._emit(msg_data)

                if isinstance(message, AssistantMessage) and message.usage:
                    turn_tokens = message.usage.get("input_tokens", 0) + message.usage.get("output_tokens", 0)
                    self.tokens_consumed += turn_tokens
                    await self.scheduler.token_budget.record_tokens(self.session_id, turn_tokens)

                if isinstance(message, ResultMessage):
                    if message.is_error:
                        success = False
                        self.error_message = str(message.result)
                    if message.usage:
                        self.tokens_consumed = message.usage.get("input_tokens", 0) + message.usage.get("output_tokens", 0)

            await self.scheduler.exit_turn(self.session_id, self.start_time, success, actual_tokens=self.tokens_consumed)
            self.status = "completed"
            await self._emit({"type": "status", "status": self.status})

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
            return {"type": "result", "result": message.result, "is_error": message.is_error, "uuid": message.uuid}
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
