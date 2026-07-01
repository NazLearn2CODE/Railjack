---
date: 2026-07-02
status: accepted
---
# ADR: Centralized 2DOT Orchestration (supervisor + workers)

## Context

`[[index]]`'s next build phase is the orchestration core (guide Phase 2):
2DOT topologies + MCP host/client/server. The blueprint (`[[agentic-os-guide]]`
§1.1) defines three coordination topologies — **Centralized** (supervisor +
workers), Hierarchical, Decentralized — and mandates Centralized first
(known task structures, 3–7 agents). §3.2 specifies MCP (Host/Client/Server)
as the tool-integration mechanism.

Scope decision (this increment): **Centralized topology only.** MCP
host/client/server for *arbitrary external* servers is deferred — the
`claude-agent-sdk` already ships full MCP *client* support
(`ClaudeAgentOptions.mcp_servers`) plus an **in-process** MCP server helper
(`create_sdk_mcp_server` + `@tool`), so a bespoke MCP stack would duplicate
the SDK (ponytail rung 2/5). The genuine new OS capability is the topology.

## Finding

The SDK offers **native subagents** (`agents` option → built-in `Task` tool).
That path is ~3 lines but **wrong for Orbiter**: SDK subagents run
CLI-internally and bypass the OS core entirely — no `HiveMindScheduler`
admission/AIMD/circuit-breaker, no security approval gate, no HMAC receipts,
no token budget. Agents that are invisible to the OS defeat the product's
purpose (`[[architecture]]`: "treats AI agents as OS processes — with
scheduling, sandboxing, and observability").

The honest lazy solution is the opposite: **a worker IS an `AgentSession`.**
It reuses the whole stack by construction. Delegation is exposed to the
supervisor as a host-executed tool via the SDK's **in-process MCP server** —
the `mcp` package (1.28.1) is already vendored as a transitive dep, so this
adds **no new dependency** and no bespoke MCP framework. The SDK's MCP client
feeds the worker's result back into the supervisor's normal tool loop.

> MCP appears here as the *mechanism* for one internal tool, not the deferred
> "connect to arbitrary external MCP servers" feature.

## Decision

Add `app/core/orchestrator.py` containing:

- **`WorkerRole`** — `dataclass(name, system_prompt, allowed_tools)`.
  `allowed_tools` defaults to the native set, which excludes `delegate`, so
  workers are **leaves** (depth capped at 1 — the definition of Centralized).
- **`Team`** — holds a shared `HiveMindScheduler` + `SecurityPolicy` +
  `worker_provider` (a plain `Provider` with **no** delegate callback) and the
  hired roles. Two methods:
  - `async delegate(role, task) -> str` — the OS-level delegation primitive.
    Spawns a worker `AgentSession` (role's system_prompt + tool subset) and
    runs it through the shared scheduler/security; returns the worker's final
    assistant text. **Never raises** — failure/over-budget/breaker-open
    returns a descriptive string so the supervisor can recover or report
    (tool-result semantics).
  - `supervisor(prompt, system_prompt) -> AgentSession` — builds the
    supervisor session whose `ClaudeSdkProvider` carries the `delegate`
    callback (→ in-process `delegate` MCP tool); `allowed_tools` gains
    `"delegate"`.

**Edits to existing modules (additive only):**
- `app/core/agent.py` — `AgentSession` gains a per-session `allowed_tools`
  (default = today's `ALLOWED_TOOLS`) and a `final_text()` helper (last
  assistant text block, used by `delegate`).
- `app/core/provider.py` — `ClaudeSdkProvider(delegate=...)` optional
  callback; when set, `stream()` registers an in-process `delegate` MCP
  server via `mcp_servers`. The **`Provider` Protocol is unchanged** —
  delegation wiring is a concrete-impl concern; `FakeProvider` ignores it.
- `app/core/__init__.py` — re-exports `Team`, `WorkerRole`.

## Reversible?

Yes. The agent/provider edits are additive (defaults preserve today's
behavior). Reverting is deleting `orchestrator.py`, dropping the
`allowed_tools`/`final_text`/`delegate` additions, and the re-exports.

## Impact

- Centralized 2DOT topology lands with workers as **first-class OS
  citizens**: admission, AIMD, circuit-breaker, rate-limit, token budget,
  security floor, approval gate, and receipts all apply to every worker.
- Delegation is provider-agnostic and **SDK-free testable** via `FakeProvider`
  (the `delegate` primitive is pure Python over `AgentSession`).
- Integration boundary (honestly marked, like the existing native-API
  deferral): the supervisor *LLM choosing* to call `delegate` is only
  exercisable with a real LLM; the OS primitive itself is fully tested.
- Deferred (YAGNI, with triggers): Hierarchical/Decentralized topologies;
  external-MCP-client integration; shared team token-budget pool;
  per-worker autonomy levels; REST `POST /api/teams` + dashboard team view;
  A2A protocol.
