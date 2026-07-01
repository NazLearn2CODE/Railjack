"""SDK-free Provider for deterministic agent-loop tests.

The second Provider implementation — earns the Provider Protocol its keep and
lets the agent loop (budget enforcement, status transitions) run with no CLI,
no network, no claude-agent-sdk subprocess.
"""
from __future__ import annotations

from typing import Any, AsyncIterator


class FakeProvider:
    """Replays a canned list of normalized event dicts. Structural Provider."""

    def __init__(self, events: list[dict[str, Any]]):
        self._events = events

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt,
        allowed_tools,
        session_id,
        on_tool_use,
    ) -> AsyncIterator[dict[str, Any]]:
        for ev in self._events:
            yield ev


class FakeGatedProvider:
    """Replays events AFTER driving one tool call through `on_tool_use`.

    FakeProvider never invokes the gate, so it can't exercise a worker whose
    gated tool (Write/Edit/Bash) must surface as an actionable approval. This one
    awaits the gate's verdict (captured on `.verdict`) then yields the canned
    events — the worker blocks inside the gate until the operator resolves it.
    """

    def __init__(self, tool_name: str, tool_input: dict[str, Any], events: list[dict[str, Any]]):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self._events = events
        self.verdict: Any = None

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt,
        allowed_tools,
        session_id,
        on_tool_use,
    ) -> AsyncIterator[dict[str, Any]]:
        self.verdict = await on_tool_use(self.tool_name, self.tool_input)
        for ev in self._events:
            yield ev
