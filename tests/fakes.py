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
