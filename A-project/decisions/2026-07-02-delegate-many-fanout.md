---
date: 2026-07-02
status: accepted
---
# ADR: delegate_many — concurrent fan-out (Centralized 2DOT, route 2)

## Context

The Centralized topology (`[[2026-07-02-centralized-2dot-topology]]`) delegates
**sequentially**: `Team.delegate()` runs one worker per call (`await worker.run()`
inline), so a supervisor decomposing into N independent subtasks waits for each
in turn. `[[index]]` named the next increment: elevate fan-out to a first-class
OS primitive so a supervisor can dispatch N workers **concurrently**. This
changes 2DOT semantics from sequential spokes to concurrent fan-out — the
trigger that makes a dedicated multi-lane view earn its keep
(`[[2026-07-02-worker-streaming-lanes]]` surfaced lanes; concurrent lanes need
grouping). Plan: `[[2026-07-02-delegate-many-fanout-plan]]`.

## Finding

Two ways to give the supervisor concurrent dispatch:

1. **Rely on the SDK to run multiple `delegate` tool_use blocks in parallel
   within one assistant turn.** *Rejected:* depends on unverified SDK tool-call
   concurrency, breaks the lanes ADR's call→execute→result temporal-ordering
   assumption (the SDK blocks the supervisor's loop for the duration of a tool
   call), and cannot be exercised without the real LLM. The project pattern is
   OS primitives proven SDK-free.
2. **A new host tool `delegate_many` that `asyncio.gather`s N workers
   internally.** Each worker is still a first-class `AgentSession` through the
   shared `HiveMindScheduler` (admission/AIMD/breaker/rate-limit/budget) +
   security gate + receipt ledger — exactly the Centralized topology, fanned
   out. Deterministic, SDK-agnostic, testable via `FakeProvider`.

Option 2 is the lazy one and matches the OS framing: one host tool = one fan-out
primitive = one scheduling event. The supervisor's loop blocks on the single
tool call while N workers stream concurrently underneath — a clean window to
group their lanes.

The scheduler is already concurrency-safe: `AdmissionControl` uses a real
`asyncio.Condition` bounded by the AIMD live limit (init 4, max 10), so a fan-out
of 8 admits ~4 and queues the rest — genuine backpressure, no extra bound
(YAGNI). AIMD/breaker/budget mutate only in await-free sections (cooperative
asyncio → no preemption); the lone benign race is a RPM TOCTOU across concurrent
`enter_turn` (may slightly over-admit RPM under fan-out — acceptable for a local
OS; fixed only if a real rate-limit event is observed).

## Decision

**Replace** `delegate` with `delegate_many` (one primitive — ponytail: no two
tools doing the same thing; the augment alternative was considered and rejected
as redundant two-tool/two-render-path surface).

- **`app/core/orchestrator.py`**
  - `async delegate_many(delegations: list[dict]) -> str` — generates
    `fanout_id = "fanout-{uuid8}"`, then
    `await asyncio.gather(*[self._run_worker(role, task, fanout_id) …])`.
    `_run_worker` never raises (failure/over-budget/crash → string), so gather
    cannot throw. Results aggregate as per-role markdown sections
    (`### {role}\n{result}`) in input order — the single tool result the
    supervisor synthesizes from.
  - `_run_worker(role, task, fanout_id) -> str` — the extracted per-worker body
    of the old `delegate` (role lookup, build worker `AgentSession`, set
    `event_sink`, register, `await worker.run()`, return final text / status
    string). The `_to_supervisor` closure now nests `fanout_id` into every
    `worker_event` frame.
  - `supervisor()` wires `ClaudeSdkProvider(delegate_many=self.delegate_many)`
    and adds `"delegate_many"` (not `"delegate"`) to `allowed_tools`.
    `default_supervisor_prompt` rewritten to instruct fan-out.
  - `import asyncio` added.
- **`app/core/provider.py`**
  - `ClaudeSdkProvider.__init__` replaces the `delegate` param with
    `delegate_many: Optional[Callable[[list[dict]], Awaitable[str]]] = None`
    (concrete-impl concern; the **`Provider` Protocol is unchanged**; `FakeProvider`
    ignores it — same posture as today). `stream()` registers the in-process
    `delegate_many` MCP tool (`{"delegations":[{"role":str,"task":str}]}`) under
    the `orbiter` server.
- **Wire** — the nested `worker_event` frame gains `fanout_id`.
- **Frontend (`web/src`)** — a new `fanout` Row kind = the grouped container
  (`▾ FAN-OUT ▸ N workers [X live · Y done]` + stacked lanes). `applyEvent`
  routes a `worker_event` (carrying `fanout_id`) into the matching `fanout`
  (find-or-create by `fanoutId`) and the lane within it (find-or-create by
  `worker_id`) — **grouped, not appended at top level**. The `delegate_many`
  tool_use and its result render as normal `tool_use`/`result` rows above/below
  the block. Each lane reuses the existing `worker_lane` renderer (now only
  nested) incl. its `ApprovalCard` via `approveWorker`. Live/done counts derive
  from lane statuses (not stored). The standalone top-level `worker_lane` path
  is removed — under replace every `worker_event` carries a `fanout_id`.
- **No change to `main.py`** (Team construction unchanged; the new callback is
  internal to `supervisor()`).

## Reversible?

Yes. The wire change is additive (`fanout_id` optional); reverting drops
`delegate_many`/`_run_worker`/`fanout` and restores `delegate` + the top-level
`worker_lane` render path. Defaults preserve single-session behavior.

## Impact

- A supervisor can now fan N independent subtasks to its workers in one call;
  they schedule, gate, and bill concurrently through the same OS core.
- The dedicated multi-lane view is no longer YAGNI — it lands here as the inline
  grouped `fanout` block (the smallest rendering that makes concurrent lanes
  legible). A separate Team panel/route stays deferred.
- **Integration boundary (honest, like the sibling ADRs):** the OS primitive is
  SDK-free tested via `FakeProvider`/`FakeBlockingProvider`; the *supervisor LLM
  choosing* to call `delegate_many` — and correctly filling the
  `delegations: [{role,task}…]` array-of-objects schema (harder than today's flat
  `{role,task}`) — is only exercisable with a real LLM. Noted as the key risk
  under the z.ai/GLM backend.
- Deferred (YAGNI, w/ triggers): a dedicated Team panel/route; per-fan-out
  token-budget pool; a fan-out concurrency cap (admission already bounds it);
  Hierarchical/Decentralized topologies.

## Test surface

- `tests/fakes.py` — `FakeBlockingProvider`: signals entry (shared counter) and
  parks on an `asyncio.Event` before yielding — proves >1 worker is in-flight
  simultaneously (sequential dispatch would deadlock at 1).
- `tests/test_orchestrator.py` — rewritten for `delegate_many`: final-text,
  unknown-role, over-budget, admission-slot release, token recording,
  leaves-have-no-`delegate_many`-tool, supervisor-has-`delegate_many`-tool,
  forwarding now tagged with `fanout_id`, gated-worker-approval (single-item
  fan-out). **New:** runs-workers-concurrently (overlap proof) and
  aggregates-results-in-input-order.
- Frontend: `tsc --noEmit` + `vite build` (no JS test runner — matches the
  existing pattern).
