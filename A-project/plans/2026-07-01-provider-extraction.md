# Provider Protocol — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a `Provider` `typing.Protocol` (the blueprint's LLM trait) so the OS core in `app/core/agent.py` depends on an abstraction, not on `claude-agent-sdk` directly. Prove the trait with a `FakeProvider` that also unlocks SDK-free agent-loop tests.

**Architecture:** A Provider yields already-normalized event dicts (`message`/`result`/`rate_limit`) — the same shapes `_serialize_message` produces today — so the agent loop becomes a dumb dict-iterator. All SDK coupling (`query`, `ClaudeAgentOptions`, `HookMatcher`, `isinstance` dispatch, `_serialize_message`, `_safe`) moves into `ClaudeSdkProvider`. OS policy (security floor, operator approval, token budget) stays in `AgentSession` and is handed to the provider as an `on_tool_use` callback. Channel is deferred (single impl today = YAGNI). Spec: `A-project/decisions/2026-07-01-provider-protocol.md`.

**Tech Stack:** Python 3.11+ (matches `pyproject.toml`), `typing.Protocol`/`AsyncIterator`, `claude-agent-sdk` 0.1.81, pytest. New module uses `from __future__ import annotations` (matches the newest core file, `sandbox.py`).

**Key correctness points (apply throughout):**
- **Behavior is preserved exactly.** `ClaudeSdkProvider` is a relocation, not a rewrite: same options, same `DANGEROUS_TOOLS` matcher, same serialization, same hook timeout (`APPROVAL_TIMEOUT + 10`). The only structural change is *where* that code lives.
- **`APPROVAL_TIMEOUT` stays in `agent.py`.** It is the operator-side fail-closed timeout (OS policy). Only the hook-level `APPROVAL_TIMEOUT + 10` (SDK plumbing) moves to the provider. `tests/test_agent_approval.py` monkeypatches `agent_mod.APPROVAL_TIMEOUT` — that must keep working.
- **`DANGEROUS_TOOLS = "Bash|Write|Edit"`** moves to `provider.py`. It must match the tools the agent wants gated (currently the fixed set Bash/Write/Edit). Add a `# ponytail:` comment noting the coupling; do **not** parameterize it — the set is stable.
- **`_on_tool_use` is the new seam** the approval test drives. The old `_pre_tool_use_hook(hook_input, tool_use_id, context) -> hookSpecificOutput dict` becomes (a) `AgentSession._on_tool_use(tool_name, tool_input) -> ToolDecision` (OS policy + approval) and (b) a thin adapter inside `ClaudeSdkProvider` that translates `ToolDecision` → the SDK's `hookSpecificOutput`. Test the policy via (a); test the translation via a small dedicated case.

---

## Chunk 1: Provider trait + ClaudeSdkProvider (extraction — one green commit)

This chunk is a relocation refactor. The intermediate state (code in two places) is not independently green, so the extraction is **one commit**. Verify with the existing suite + a focused import/translation test added in Chunk 3.

### Task 1: Create `app/core/provider.py`

**Files:**
- Create: `app/core/provider.py`
- Reference (relocate from): `app/core/agent.py:7-8,24-29,210-224,228-243,269-331`

- [ ] **Step 1: Write the module — `ToolDecision`, `Provider` Protocol, `ClaudeSdkProvider`**

```python
# app/core/provider.py
"""Blueprint §2.1 microkernel trait: the LLM Provider.

The OS core depends on this Protocol, not on claude-agent-sdk. ClaudeSdkProvider
is the concrete impl; FakeProvider (tests/fakes.py) is the second impl that earns
the trait and enables SDK-free agent-loop tests. See ADR 2026-07-01-provider-protocol.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, NamedTuple, Optional, Protocol

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, ResultMessage, UserMessage, RateLimitEvent, HookMatcher

logger = logging.getLogger("orbiter.provider")

# Tools that trigger the PreToolUse approval gate. SDK-hook plumbing — lives here
# because it is the provider's job to register the hook.
# ponytail: not parameterized; must match the agent's gated set (Bash/Write/Edit).
DANGEROUS_TOOLS = "Bash|Write|Edit"

# The CLI waits slightly longer than the agent's operator-timeout (agent.APPROVAL_TIMEOUT + 10)
# so our fail-closed deadline fires before the hook itself times out.
HOOK_TIMEOUT_MARGIN = 10.0


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

    async def stream(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str],
        allowed_tools: list[str],
        session_id: str,
        on_tool_use: OnToolUse,
    ) -> AsyncIterator[dict[str, Any]]:
        # Adapter: translate the OS-policy verdict into the SDK's hook protocol.
        async def _hook(hook_input: dict, tool_use_id: Optional[str], context) -> dict:
            verdict = await on_tool_use(hook_input.get("tool_name", "?"), hook_input.get("tool_input", {}))
            out = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": "allow" if verdict.allow else "deny"}}
            if not verdict.allow:
                out["hookSpecificOutput"]["permissionDecisionReason"] = verdict.reason
            return out

        # APPROVAL_TIMEOUT is imported lazily from agent to avoid a circular import
        # (agent imports Provider for typing). It is the operator-side fail-closed
        # timeout; the hook waits marginally longer (see HOOK_TIMEOUT_MARGIN).
        from app.core.agent import APPROVAL_TIMEOUT

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            hooks={"PreToolUse": [HookMatcher(matcher=DANGEROUS_TOOLS, hooks=[_hook],
                                               timeout=APPROVAL_TIMEOUT + HOOK_TIMEOUT_MARGIN)]},
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
                    content_blocks.append({"type": "tool_use", "tool_use_id": block.id,
                                           "name": block.name, "input": getattr(block, "input", {})})
            return {"type": "message", "role": "assistant", "content": content_blocks,
                    "uuid": message.uuid, "usage": message.usage}
        elif isinstance(message, ResultMessage):
            return {"type": "result", "result": message.result, "is_error": message.is_error,
                    "usage": message.usage, "uuid": message.uuid}
        elif isinstance(message, RateLimitEvent):
            return {"type": "rate_limit", "rate_limit_type": message.rate_limit_type,
                    "info": getattr(message, "info", None)}
        return None
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `.venv/bin/python -c "from app.core.provider import Provider, ClaudeSdkProvider, ToolDecision; print('ok')"`
Expected: prints `ok` (no import error, no circular import).

### Task 2: Decouple `app/core/agent.py` to use the Provider

**Files:**
- Modify: `app/core/agent.py` (remove SDK imports + relocated helpers; rewrite `run()` and the hook)

- [ ] **Step 1: Rewrite the SDK-touching parts of `agent.py`**

Changes:
1. **Imports:** drop `from claude_agent_sdk import query, ClaudeAgentOptions` and the `claude_agent_sdk.types` import. Add `from app.core.provider import Provider, ClaudeSdkProvider, ToolDecision`. Keep `from app.core.scheduler import HiveMindScheduler` and `from app.core.security import SecurityPolicy`.
2. **Constants:** delete `DANGEROUS_TOOLS` (now in provider). Keep `ALLOWED_TOOLS` and `APPROVAL_TIMEOUT`.
3. **Delete** `_prompt_stream`, `_safe`, `_serialize_message` (relocated to provider). Keep `_throughput` — it reads the normalized `usage` dict, which is agent-side budget accounting.
4. **`AgentSession.__init__`:** add `provider: Optional[Provider] = None` param; store `self.provider = provider or ClaudeSdkProvider()`.
5. **Replace `_pre_tool_use_hook`** with `_on_tool_use` (returns `ToolDecision`, no SDK dict):

```python
async def _on_tool_use(self, tool_name: str, tool_input: dict) -> ToolDecision:
    """OS-policy gate for dangerous tools: security floor, then operator approval.

    Returns a ToolDecision the provider translates into the SDK hook protocol.
    """
    # 1. Policy floor (L1/L2) — catastrophic actions hard-deny, never reaching the operator.
    if self.security is not None:
        decision = self.security.evaluate(tool_name, tool_input, self.session_id)
        if decision.hard_deny:
            self.security.mint_and_append_receipt(self.session_id, tool_name, tool_input, "deny")
            logger.warning("Session %s policy-denied %s: %s", self.session_id, tool_name, decision.reason)
            return ToolDecision(allow=False, reason=f"Blocked by Orbiter security policy: {decision.reason}")

    # 2. Operator approval — surface and block, fail-closed on timeout.
    approval_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    self.pending_approvals[approval_id] = future
    self.status = "waiting_approval"
    logger.warning("Session %s requesting approval for %s", self.session_id, tool_name)
    await self._emit({"type": "approval_needed", "approval_id": approval_id, "tool": tool_name, "input": tool_input})

    try:
        approved = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Approval %s timed out after %ss — denying", approval_id, APPROVAL_TIMEOUT)
        approved = False
    finally:
        self.pending_approvals.pop(approval_id, None)
        self.status = "running"

    if self.security is not None:
        self.security.mint_and_append_receipt(self.session_id, tool_name, tool_input, "allow" if approved else "deny")

    if approved:
        return ToolDecision(allow=True)
    return ToolDecision(allow=False, reason="Execution denied by operator via dashboard (or approval timed out).")
```

6. **Rewrite `run()`'s query loop** to iterate the provider's normalized dicts (drop `isinstance` dispatch — the dicts already carry `type`/`usage`/`is_error`):

```python
async for event in self.provider.stream(
    self.prompt,
    system_prompt=self.system_prompt,
    allowed_tools=ALLOWED_TOOLS,
    session_id=self.session_id,
    on_tool_use=self._on_tool_use,
):
    self.messages.append(event)
    await self._emit(event)

    if event.get("type") == "message" and event.get("role") == "assistant" and event.get("usage"):
        used = self._throughput(event["usage"])
        self.tokens_consumed += used
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
```

(Remove the now-unused `AssistantMessage`/`ResultMessage`/`message.usage` branches and the `query(...)`/`options` block they replaced.)

7. **`AgentSessionManager.__init__`:** add `provider: Optional[Provider] = None`; pass `provider=provider` into `AgentSession(...)` in `create_session`.

- [ ] **Step 2: Re-export from the core package**

Modify `app/core/__init__.py` — append to the docstring's intent:
```python
"""OS core: HiveMind scheduler primitives + agent runner + Provider trait."""
from app.core.provider import Provider  # noqa: F401 — re-export the LLM trait
```

- [ ] **Step 3: Run the suite — expect the approval test to FAIL, everything else green**

Run: `.venv/bin/pytest -q`
Expected: `test_agent_approval.py` tests fail (they call the removed `_pre_tool_use_hook`); scheduler/security/sandbox/health tests pass. This confirms the wiring is otherwise intact.

- [ ] **Step 4: Commit the extraction**

```bash
git add app/core/provider.py app/core/agent.py app/core/__init__.py
git commit -m "refactor(security): extract Provider trait, decouple agent from claude-agent-sdk

OS core now depends on the Provider Protocol, not the SDK. ClaudeSdkProvider
absorbs all SDK coupling (query/options/hook/serialize); FakeProvider is the
second impl. Behavior preserved. Approval test rewrite + FakeProvider next."
```

---

## Chunk 2: Rewrite the approval test for the new seam + cover the adapter

### Task 3: Rewrite `tests/test_agent_approval.py` to drive `_on_tool_use`

**Files:**
- Modify: `tests/test_agent_approval.py`

- [ ] **Step 1: Rewrite the three scenarios against `ToolDecision`**

The test's *intent* is unchanged — verify the gate logic (approve→allow, deny→deny, timeout→deny, policy floor short-circuits). It now drives `_on_tool_use(tool_name, tool_input)` instead of `_pre_tool_use_hook(hook_input, tu_id, context)` and asserts on `ToolDecision.allow`/`.reason`:

```python
# allow/deny: schedule _on_tool_use, let it emit + park the future, resolve via approve_tool.
task = asyncio.ensure_future(s._on_tool_use("Bash", {"command": "echo hi"}))
await asyncio.sleep(0)
assert s.status == "waiting_approval"
approval_id = next(iter(s.pending_approvals))
await s.approve_tool(approval_id, True)
verdict = await asyncio.wait_for(task, timeout=2.0)
assert verdict.allow is True

# deny path: approve(False) → assert verdict.allow is False and verdict.reason set.
# timeout: patch agent_mod.APPROVAL_TIMEOUT = 0.1 as today; assert verdict.allow is False
#          and s.pending_approvals == {}.
# policy floor: secured session, _on_tool_use("Bash", {"command": "rm -rf /"}) →
#          assert verdict.allow is False, "security policy" in verdict.reason.lower(),
#          s.status != "waiting_approval", s.events.empty().
```

Update the module docstring's "checks the gate LOGIC" line to reference `_on_tool_use`/`ToolDecision`. Keep the `__main__` self-check block.

- [ ] **Step 2: Run — expect green**

Run: `.venv/bin/pytest tests/test_agent_approval.py -q`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_agent_approval.py
git commit -m "test(agent): drive approval gate via _on_tool_use/ToolDecision seam"
```

### Task 4: Cover the SDK hook adapter translation

**Files:**
- Create or extend: `tests/test_provider.py`

- [ ] **Step 1: Write the failing test** — the `_hook` adapter inside `ClaudeSdkProvider.stream` translates a `ToolDecision` to the right `hookSpecificOutput`. Since `_hook` is a closure, test it through a stub `stream` that captures it: construct a `ClaudeSdkProvider`, but rather than calling `stream` (which needs the SDK), assert the translation logic by mirroring the adapter via a tiny inline coroutine is over-engineering — **instead** extract the adapter to a static method so it is directly testable:

Refine Task 1 Step 1: pull the adapter out of the closure into a static method:

```python
@staticmethod
def _verdict_to_hook_output(verdict: ToolDecision) -> dict:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "allow" if verdict.allow else "deny"}}
    if not verdict.allow:
        out["hookSpecificOutput"]["permissionDecisionReason"] = verdict.reason
    return out
```

…and have `_hook` call `return ClaudeSdkProvider._verdict_to_hook_output(await on_tool_use(...))`.

```python
# tests/test_provider.py
from app.core.provider import ClaudeSdkProvider, ToolDecision

def test_verdict_allow_maps_to_allow():
    out = ClaudeSdkProvider._verdict_to_hook_output(ToolDecision(allow=True))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in out["hookSpecificOutput"]

def test_verdict_deny_carries_reason():
    out = ClaudeSdkProvider._verdict_to_hook_output(ToolDecision(allow=False, reason="nope"))
    h = out["hookSpecificOutput"]
    assert h["permissionDecision"] == "deny"
    assert h["permissionDecisionReason"] == "nope"
```

- [ ] **Step 2: Run — expect green**

Run: `.venv/bin/pytest tests/test_provider.py -q`
Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add app/core/provider.py tests/test_provider.py
git commit -m "test(provider): cover ToolDecision → SDK hook-output translation"
```

---

## Chunk 3: FakeProvider + SDK-free agent-loop tests (the payoff)

### Task 5: `FakeProvider` in `tests/fakes.py`

**Files:**
- Create: `tests/fakes.py`

- [ ] **Step 1: Write the failing test** — a `FakeProvider` yields a canned event list, including an assistant message whose `usage` crosses a small budget ceiling.

```python
# tests/test_agent_loop_fake.py
import asyncio
from app.core.agent import AgentSession
from app.core.scheduler import HiveMindScheduler
from tests.fakes import FakeProvider

def test_fake_session_completes_and_counts_tokens():
    async def go():
        s = AgentSession("s-fake", "hi", HiveMindScheduler(default_ceiling=10**9),
                         provider=FakeProvider([{"type": "result", "result": "ok", "is_error": False,
                                                 "usage": {"input_tokens": 100, "output_tokens": 5}}]))
        await s.run()
        assert s.status == "completed"
        assert s.tokens_consumed == 105
    asyncio.run(go())
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tests.fakes`)

Run: `.venv/bin/pytest tests/test_agent_loop_fake.py -q`
Expected: FAIL — no `tests.fakes`.

- [ ] **Step 3: Implement `FakeProvider`**

```python
# tests/fakes.py
"""SDK-free Provider for deterministic agent-loop tests. The second Provider impl."""
from __future__ import annotations
from typing import Any, AsyncIterator, Optional

from app.core.provider import OnToolUse


class FakeProvider:
    """Replays a canned list of normalized event dicts. Structural Provider."""
    def __init__(self, events: list[dict[str, Any]]):
        self._events = events
        self.recorded_calls: list[tuple[str, dict[str, Any]]] = []

    async def stream(self, prompt, *, system_prompt, allowed_tools, session_id, on_tool_use) -> AsyncIterator[dict[str, Any]]:
        for ev in self._events:
            yield ev
```

- [ ] **Step 4: Run — expect green**

Run: `.venv/bin/pytest tests/test_agent_loop_fake.py -q`
Expected: 1 passed.

- [ ] **Step 5: Add a budget-enforcement test** — a canned usage that crosses the ceiling trips `over_budget`:

```python
def test_fake_session_stops_on_budget_breach():
    async def go():
        s = AgentSession("s-budget", "hi", HiveMindScheduler(default_ceiling=50),
                         provider=FakeProvider([
                             {"type": "message", "role": "assistant", "content": [], "uuid": "u1",
                              "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                                        "cache_creation_input_tokens": 0, "output_tokens": 0}},
                             {"type": "result", "result": "never reached", "is_error": False, "usage": {}},
                         ]))
        await s.run()
        assert s.status == "failed"
        assert "budget" in (s.error_message or "").lower()
    asyncio.run(go())
```

Run: `.venv/bin/pytest tests/test_agent_loop_fake.py -q` → Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/fakes.py tests/test_agent_loop_fake.py
git commit -m "test(agent): SDK-free loop + budget-enforcement tests via FakeProvider"
```

---

## Chunk 4: Verify + docs

### Task 6: Full-suite green + docs sync

**Files:**
- Verify: full `pytest` suite
- Modify: `A-project/index.md`, `A-project/architecture.md`

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all green (existing 5 files + new `test_provider.py` + `test_agent_loop_fake.py`).

- [ ] **Step 2: Confirm no frontend/typecheck regression** (no web changes expected — sanity only)

Run: `cd web && npm run build` (only if `web/dist` is part of the surface; skip if node_modules absent)
Expected: build unchanged (this refactor touches only the Python core).

- [ ] **Step 3: Update `index.md` status** — move "protocol-based core" from "Next stage" to shipped: note `Provider` trait landed (`app/core/provider.py`), `ClaudeSdkProvider` + `FakeProvider`, Channel + orchestration now the remaining "Next stage".

- [ ] **Step 4: Update `architecture.md`** — in the Blueprint→Python table, mark the `Provider` Protocol row as DONE; add a one-line `app/core/provider.py` to the OS-core box.

- [ ] **Step 5: Commit docs**

```bash
git add A-project/index.md A-project/architecture.md
git commit -m "docs: Provider trait landed — core SDK-free; Channel/orchestration next"
```

---

## Notes for the executor
- **No circular import:** `provider.py` imports `claude_agent_sdk` (fine) and lazily imports `APPROVAL_TIMEOUT` from `agent.py` *inside* `stream()`; `agent.py` imports `Provider`/`ClaudeSdkProvider`/`ToolDecision` from `provider.py` at module top. The lazy inner import breaks the cycle.
- **Don't add a `providers/` package** — one module is correct until a second *real* provider arrives.
- **`save_log` keeps working** unchanged: it reads the same event-dict shapes, now produced by `ClaudeSdkProvider._serialize_message`.
