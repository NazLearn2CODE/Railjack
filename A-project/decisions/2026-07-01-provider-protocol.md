---
date: 2026-07-01
status: accepted
---

# ADR: Protocol-based Provider (LLM trait abstraction)

## Context

The blueprint (`[[agentic-os-guide]]` §2.1) mandates a "microkernel-shaped"
core whose runtime "depends only on abstract traits (ABI) rather than concrete
implementations of providers (LLMs) or channels (messaging)."
`[[architecture]]` maps this to `typing.Protocol`s for `Provider` (LLM) and
`Channel` (messaging).

Today `app/core/agent.py` is welded to `claude-agent-sdk`: it imports
`query`, `ClaudeAgentOptions`, and four `claude_agent_sdk.types`, builds options
inline, runs `async for message in query(...)`, and dispatches on
`isinstance(message, AssistantMessage|ResultMessage|UserMessage|RateLimitEvent)`.
The OS core therefore depends on a concrete SDK, not a trait — the one piece of
the microkernel mandate left unaddressed after the security floor shipped
(`[[2026-07-01-security-l1-l2-l4]]`, `[[2026-07-01-sandbox-l3-landlock]]`).

Per `[[index]]`'s "next stage", this increment introduces the **Provider**
trait. **Channel is deferred** — it has a single implementation today (the
WebSocket gateway), so abstracting it now would be an interface with one
consumer and no second impl (YAGNI).

## Finding

`agent.py` already normalizes every SDK message to a plain dict before use:
`_serialize_message` + `_safe` produce the `{type: message|result|rate_limit,
...}` shapes that `_emit`, `save_log`, and token-budget accounting consume. The
SDK types never escape that boundary. So the natural seam is **after**
normalization: a Provider that *yields normalized event dicts*, not SDK objects.

The operator-approval + security-floor logic (`_pre_tool_use_hook`) is OS
policy, not provider mechanics — it must stay in the agent. The SDK-specific
part of that hook is only the `HookMatcher`/`permissionDecision` plumbing, which
is pure adaptation and belongs in the provider.

## Decision

Add `app/core/provider.py` containing:

- **`Provider`** — a `typing.Protocol` with one method:
  ```python
  async def stream(self, prompt, *, system_prompt, allowed_tools, session_id,
                   on_tool_use: Callable[[str, dict], Awaitable[ToolDecision]])
      -> AsyncIterator[dict]
  ```
  The event-dict contract is documented in the Protocol docstring (the existing
  `message`/`result`/`rate_limit` shapes) — not a `TypedDict` union (concise
  first; promote if a second provider needs enforcement).
- **`ToolDecision`** — `NamedTuple(allow: bool, reason: str = "")`, the verdict
  the agent returns from `on_tool_use`.
- **`ClaudeSdkProvider`** — the concrete impl. Absorbs `query`, the SDK imports,
  `ClaudeAgentOptions`, `_serialize_message`, `_safe`, `DANGEROUS_TOOLS`, and
  the hook-level `APPROVAL_TIMEOUT+10`. Its PreToolUse handler is a thin adapter:
  await `on_tool_use(name, input)` → translate the `ToolDecision` into the SDK's
  `permissionDecision`/`permissionDecisionReason` dict.

**What stays in `AgentSession`** (provider-agnostic OS policy): the security
floor (`security.evaluate` → hard-deny), operator approval (emit
`approval_needed`, park the future, fail-closed `APPROVAL_TIMEOUT`), token
budget, `save_log`, the event bus. These become the body of the `on_tool_use`
callback the session hands to `provider.stream(...)`.

**Second implementation** (the trait earns its keep): `FakeProvider` in
`tests/fakes.py`, yielding canned dicts — enables fast, deterministic,
SDK- and network-free agent-loop tests. A `Protocol` with one impl is slop; the
fake is the honest second impl.

**Wiring:** `AgentSessionManager` holds a `Provider` (default
`ClaudeSdkProvider()`); `main.py` is unchanged (uses the default); tests inject
`FakeProvider`. `app/core/__init__.py` re-exports `Provider`.

A `providers/` package is **not** created — one module holds the Protocol and
its sole concrete impl. Split only when the native-Anthropic-API provider
actually lands (the lower-priority cross-check noted in `[[index]]`).

## Reversible?

Yes — `ClaudeSdkProvider` preserves today's behavior exactly (same options,
same hook, same serialization, just relocated). Reverting is deleting
`provider.py` and inlining its SDK calls back into `agent.py`.

## Impact

- `agent.py` imports nothing from `claude_agent_sdk`; the OS core depends on the
  `Provider` trait, satisfying the microkernel mandate for the LLM axis.
- External behavior unchanged: `run()` calls `provider.stream(...)` and the
  yielded dicts flow through the existing emit/budget/`save_log` paths.
- `FakeProvider` unblocks SDK-free agent-loop tests (budget enforcement,
  approval callback, status transitions) without spinning the CLI or network.
- Channel trait + orchestration (MCP host/client/server, 2DOT) remain next.
