---
date: 2026-07-02
status: accepted
---
# ADR: Worker streaming lanes (team observability)

## Context

Once the centralized topology surfaced (`[[2026-07-02-centralized-2dot-topology]]`),
team runs were observable only at the supervisor level: the dashboard showed the
supervisor's `delegate` tool call and its string result, but the **worker's inner
activity** (reasoning, tool calls, results) was invisible — worker events went to the
worker's *own* unconsumed `events` queue and only `final_text()` returned. The index
named "richer team observability" as the next increment.

## Finding

Two ways to surface worker runs:

1. **Register workers + a WS per worker** — new endpoint(s), frontend multi-WS
   management, worker lifecycle in the session list. Over-built for the goal.
2. **Forward worker events onto the supervisor's bus** — the supervisor already has a
   `/ws/sessions/{id}` stream and a replay path (`GET /api/sessions/{id}`). Nesting
   worker events under a `worker_event` frame reuses all of it. No new endpoint.

Option 2 is the lazy one and preserves temporal ordering: the supervisor's provider
streams the `delegate` tool-use, the SDK then executes the tool (the worker runs,
forwarding as it goes), then the tool result returns — so the lane naturally appears
between the call and the result on the same stream.

## Decision

- **`AgentSession.event_sink`** (`app/core/agent.py`) — an optional
  `Callable[[event], Awaitable[None]]`; `_emit` forwards every emitted event to it in
  addition to its own queue. **`AgentSession.ingest(event)`** — appends an externally-
  produced event to `messages` *and* `events` (so it streams live **and** replays),
  without re-forwarding to the session's own sink (no recursion; uses `events.put`
  directly).
- **`Team._supervisor`** (`app/core/orchestrator.py`) — set by `supervisor()`.
  `delegate()` sets each worker's `event_sink` to a closure that wraps the event as
  `{"type": "worker_event", "role", "worker_id", "event"}` and calls
  `supervisor.ingest(...)`.
- **Frontend** (`web/src`) — `worker_event` on `StreamEvent`; a `worker_lane` `Row`.
  `applyEvent` routes inner events into an immutable lane row keyed by `worker_id`
  (find-or-create, copy-on-write). `Message` renders the lane as a nested HUD
  sub-panel (`◂ DELEGATED · role` + status pip), reusing `Message` for inner rows.
  Worker `approval_needed` → a muted `⏸ WORKER GATE` indicator (see Honest gap).

The nested `stream_end` stays under `event.type` (top-level type is `worker_event`), so
it cannot terminate the supervisor's WS drain loop.

## Reversible?

Yes, fully additive. Reverting drops `event_sink`/`ingest` from `agent.py`, the
`_supervisor` field + sink closure from `orchestrator.py`, the `worker_event`/
`worker_lane` types + reducer case + `Message` case, and the forwarding test. Defaults
preserve today's behavior (no sink → no forwarding).

## Impact

- Worker reasoning, tool calls, and results stream live in context and replay on
  session select — the core observability gap is closed with **no new endpoint**.
- **Honest gap (deliberately deferred):** workers run through the security gate but are
  **not registered** with `AgentSessionManager`, so `POST /api/sessions/{worker_id}`
  /`approve` does not resolve a worker's gate. A worker's gated tool (e.g. the `coder`
  role's Write/Edit/Bash) therefore blocks until `APPROVAL_TIMEOUT` (600s) → fail-closed
  deny → returns an error string to the supervisor. The lane shows this as a muted
  indicator rather than a non-functional button. *Next increment:* register transient
  workers (filtered from `list_sessions`) so the lane can show a real, actionable
  `ApprovalCard`. Read-only workers (the `researcher` role) are fully functional today.
- **Integration boundary:** the forwarding primitive is SDK-free tested via
  `FakeProvider`; the *supervisor LLM choosing* to call `delegate` remains a real-LLM
  boundary (same as the topology ADR).

## Test surface

- `tests/test_orchestrator.py` — `test_delegate_forwards_worker_events_to_supervisor_bus`:
  forwarded frames are nested `worker_event`s tagged with role + worker_id, carry the
  worker's message/result, the nested `stream_end` stays nested, and they persist in
  `supervisor.messages` for replay.
- Frontend: `tsc --noEmit` + `vite build` gate types/rendering (no JS test runner in the
  project — matches the existing frontend verification pattern).
