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


class FakeBlockingProvider:
    """Counts concurrent entries and parks on `release` before yielding.

    Proves Team.delegate_many dispatches workers concurrently: the entry counter
    reaches N only if N workers ran their provider at once. Sequential dispatch
    parks worker 1 on `release` and never starts worker 2, so the counter stalls
    at 1 → the test's wait times out (the red state under sequential).

    `counter` is a 1-element list (mutable holder) shared across the N workers
    that reuse this one provider instance; `release` is an asyncio.Event the test
    sets once all workers are observed in-flight.
    """

    def __init__(self, events: list[dict[str, Any]], counter: list[int], release: Any):
        self._events = events
        self._counter = counter
        self._release = release

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt,
        allowed_tools,
        session_id,
        on_tool_use,
    ) -> AsyncIterator[dict[str, Any]]:
        self._counter[0] += 1
        await self._release.wait()
        for ev in self._events:
            yield ev
