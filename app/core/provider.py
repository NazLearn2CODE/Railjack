"""Blueprint §2.1 microkernel trait: the LLM Provider.

The OS core depends on this Protocol, not on claude-agent-sdk. ClaudeSdkProvider
is the concrete impl; FakeProvider (tests/fakes.py) is the second impl that earns
the trait and enables SDK-free agent-loop tests. See ADR 2026-07-01-provider-protocol.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Awaitable, Callable, NamedTuple, Optional, Protocol

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, RateLimitEvent, ResultMessage, UserMessage, HookMatcher

logger = logging.getLogger("orbiter.provider")

# Tools that trigger the PreToolUse approval gate. SDK-hook plumbing — lives here
# because it is the provider's job to register the hook.
# ponytail: not parameterized; must match the agent's gated set (Bash/Write/Edit).
DANGEROUS_TOOLS = "Bash|Write|Edit"


class ToolDecision(NamedTuple):
    """Verdict the agent returns from on_tool_use: allow the tool call, or deny (+reason)."""
    allow: bool
    reason: str = ""


# on_tool_use(tool_name, tool_input) -> verdict. Invoked by the provider for any
# tool matching DANGEROUS_TOOLS; the provider blocks on it and honors the verdict.
OnToolUse = Callable[[str, dict[str, Any]], Awaitable[ToolDecision]]


class Provider(Protocol):
    """LLM trait. Yields normalized provider events as plain dicts so the core
    never imports SDK types.

    Event-dict contract (the shapes the agent loop, _emit, and save_log consume):
      - {"type": "message", "role": "user"|"assistant", "content": str|list[block],
         "uuid": str, "usage": dict|None}            # usage present on assistant only
      - {"type": "result", "result": str, "is_error": bool, "usage": dict|None, "uuid": str}
      - {"type": "rate_limit", "rate_limit_type": str, "info": Any}
    """

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str],
        allowed_tools: list[str],
        session_id: str,
        on_tool_use: OnToolUse,
    ) -> AsyncIterator[dict[str, Any]]:
        ...


async def _prompt_stream(prompt: str):
    """Yields the user prompt as a stream-json message (streaming mode)."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


class ClaudeSdkProvider:
    """Provider backed by claude-agent-sdk. Absorbs all SDK coupling from agent.py."""

    @staticmethod
    def _verdict_to_hook_output(verdict: ToolDecision) -> dict:
        """Translates an OS-policy verdict into the SDK's PreToolUse hook protocol."""
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if verdict.allow else "deny",
            }
        }
        if not verdict.allow:
            out["hookSpecificOutput"]["permissionDecisionReason"] = verdict.reason
        return out

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str],
        allowed_tools: list[str],
        session_id: str,
        on_tool_use: OnToolUse,
    ) -> AsyncIterator[dict[str, Any]]:
        # Adapter: await the OS-policy verdict, then translate it for the SDK.
        async def _hook(hook_input: dict, tool_use_id: Optional[str], context) -> dict:
            verdict = await on_tool_use(hook_input.get("tool_name", "?"), hook_input.get("tool_input", {}))
            return ClaudeSdkProvider._verdict_to_hook_output(verdict)

        # APPROVAL_TIMEOUT is imported lazily from agent to avoid a circular import
        # (agent imports Provider for typing). It is the operator-side fail-closed
        # timeout; the hook waits marginally longer (see HOOK_TIMEOUT_MARGIN).
        from app.core.agent import APPROVAL_TIMEOUT

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=DANGEROUS_TOOLS,
                        hooks=[_hook],
                        timeout=APPROVAL_TIMEOUT + 10,  # CLI waits slightly longer than our fail-closed deadline
                    )
                ]
            },
            setting_sources=[],
            session_id=session_id,
        )

        async for message in query(prompt=_prompt_stream(prompt), options=options):
            serialized = self._serialize_message(message)
            if serialized is not None:
                yield serialized

    # --- relocated verbatim from agent.py (pure helpers) ---
    @staticmethod
    def _safe(obj: Any) -> Any:
        """Recursively coerce SDK block objects into JSON-serializable structures."""
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, list):
            return [ClaudeSdkProvider._safe(x) for x in obj]
        if isinstance(obj, dict):
            return {k: ClaudeSdkProvider._safe(v) for k, v in obj.items()}
        if hasattr(obj, "model_dump"):  # pydantic
            return obj.model_dump()
        if hasattr(obj, "__dataclass_fields__"):  # dataclass (ToolResultBlock, etc.)
            return {k: ClaudeSdkProvider._safe(getattr(obj, k)) for k in obj.__dataclass_fields__}
        return str(obj)

    def _serialize_message(self, message: Any) -> Optional[dict[str, Any]]:
        """Serializes Claude SDK Message types to JSON-friendly dicts."""
        if isinstance(message, UserMessage):
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
