---
date: 2026-07-02
status: accepted
---
# ADR: Actionable worker approvals

## Context

Worker streaming lanes (`[[2026-07-02-worker-streaming-lanes]]`) closed the
*observability* gap but explicitly left a *control* gap: workers run through the
security/approval gate but were **not registered** with `AgentSessionManager`, so
`POST /api/sessions/{worker_id}/approve` could not resolve a worker's gate. A
worker's gated tool (the `coder` role's Write/Edit/Bash) therefore blocked until
`APPROVAL_TIMEOUT` (600s) → fail-closed deny → an error string back to the
supervisor. The lane rendered only a muted `⏸ WORKER GATE` indicator.

## Finding

Two ways to make a worker's gate actionable:

1. **A dedicated worker-approval endpoint / WS per worker** — new surface,
   frontend multi-WS bookkeeping, worker lifecycle exposed in the sidebar.
2. **Register the worker into the existing manager**, then reuse the *already
   proven* `POST /api/sessions/{id}/approve` + `/ws` + `GET detail` paths
   unchanged. No new endpoint.

Option 2 is the lazy one: the worker's `approve_tool()` is the same method the
REST handler calls, so addressing approval by `worker_id` "just works" once the
worker lives in `manager.sessions`. The only new affordance is hiding workers
from `list_sessions()` so the sidebar stays clean (a worker is observed via its
supervisor's `worker_lane`, not as a top-level session).

## Decision

- **`Team(register=...)`** (`app/core/orchestrator.py`) — an optional
  `Callable[[AgentSession], None]`, mirroring the `delegate`-callback pattern;
  wired to `manager.register` in `main.py:create_team`. `delegate()` sets
  `kind="worker"` and calls `register(worker)` before `worker.run()`.
- **`AgentSessionManager.list_sessions()`** (`app/core/agent.py`) — excludes
  `kind == "worker"`. Workers remain reachable via `get_session()` (so `/approve`
  + `/ws` + GET detail drive them) but never appear in the sidebar list.
- **Frontend** (`web/src`) — the `worker_lane` now captures the full forwarded
  approval (`{approvalId, tool, input}`), and renders a real **shared
  `ApprovalCard`** (extracted from `Console.tsx` into its own presentational
  component, bound via `onResolve`). A new `approveWorker(workerId, approvalId,
  approve)` store action POSTs to `/api/sessions/{workerId}/approve` and clears
  the lane card on click — same optimistic UX as a top-level approval.

The shared surface means a worker's gate is resolved by the exact code path an
operator already trusts for single-agent runs; depth is still capped at 1
(workers have no `delegate` tool).

## Reversible?

Yes, fully additive. Revert drops the `register` param + the `kind="worker"` /
register call from `orchestrator.py`, the `list_sessions` filter, the
`approveWorker` action + shared `ApprovalCard` + lane capture, and the test.
Without `register`, `Team` behaves exactly as before (worker unregistered →
gate times out fail-closed).

## Impact

- A `coder` worker's Write/Edit/Bash is now operator-approvable inline in the
  supervisor's lane — the muted indicator becomes a working APPROVE/DENY card.
  Read-only workers (the `researcher` role) are unchanged.
- **Lifecycle note:** registered workers accumulate in `manager.sessions` for the
  process lifetime (same as any historical session). Acceptable for a local
  single-user dev OS; `list_sessions` keeps the sidebar clean. No GC added (YAGNI
  until a long-running process makes it matter).
- **WS re-entrancy:** a worker registered in the manager is technically
  reachable via `/ws/sessions/{workerId}` (which would re-`run()` it), but
  workers are absent from `list_sessions` and the UI never opens a WS to one —
  approval is REST. Guarding the WS against `kind == "worker"` is deliberately
  not added (unreachable via the dashboard).
- **Integration boundary:** the register→approve→resolve flow is SDK-free tested
  via `FakeGatedProvider`; the *supervisor LLM choosing* to call `delegate` on a
  gated worker remains a real-LLM boundary (same as the topology / lanes ADRs).

## Test surface

- `tests/fakes.py` — `FakeGatedProvider`: replays events after driving one tool
  call through `on_tool_use` (so the gate actually fires); captures the verdict.
- `tests/test_orchestrator.py` — `test_delegate_registers_worker_so_approval_is_actionable`:
  a gated `coder` worker registers (`kind == "worker"`), its `approval_needed` is
  resolved via `approve_tool()` (the REST path), `delegate` completes, and
  `list_sessions()` hides it.
- Frontend: `tsc --noEmit` + `vite build` gate types/rendering (no JS test runner
  in the project — matches the existing frontend verification pattern).
