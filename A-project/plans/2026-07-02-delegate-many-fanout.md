# delegate_many Fan-out — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `Team.delegate` (sequential, one worker per call) with `Team.delegate_many` — a concurrent fan-out primitive (`asyncio.gather` over N workers), each worker still a first-class `AgentSession` through the shared scheduler/security/gate/receipts. Surface it to the supervisor as a single in-process `delegate_many` MCP tool, and render N concurrent worker lanes as a grouped `fanout` block in the dashboard. Chosen route: **replace** (not augment). Spec: `A-project/decisions/2026-07-02-delegate-many-fanout.md`.

**Architecture:** `delegate_many(delegations: list[{role, task}]) -> str` generates a `fanout_id`, `asyncio.gather`s `_run_worker(role, task, fanout_id)` per delegation, and returns per-role markdown sections in input order. `_run_worker` is the extracted body of the old `delegate` (never raises). The `_to_supervisor` event-sink closure nests `fanout_id` into every `worker_event` frame; the frontend groups lanes by it. The scheduler already bounds concurrency (AIMD admission, init 4 / max 10) — no new bound. The `Provider` Protocol is unchanged; `delegate_many` is a concrete-impl callback on `ClaudeSdkProvider` (like `delegate` today).

**Tech Stack:** Python 3.11+ (`asyncio.gather`), `claude-agent-sdk` 0.1.81 (`create_sdk_mcp_server` + `tool`), pytest. React 19 + TS + Zustand + Tailwind v4 on the frontend.

**Key correctness points (apply throughout):**
- **`_run_worker` never raises.** Failure / over-budget / crash → descriptive string. Therefore `asyncio.gather(*_run_worker…)` cannot throw; a mixed fan-out returns all N sections.
- **`fanout_id` is the grouping key, end-to-end.** Generated once per `delegate_many` call; threaded into every `worker_event` frame; the frontend nests lanes by it. Under replace it is always present on `worker_event` — the top-level `worker_lane` render path is removed.
- **Replaces `delegate`, does not augment.** Remove `Team.delegate`, the provider's `delegate` ctor param + tool, the `"delegate"` in `allowed_tools`, and the top-level `worker_lane` Row emission. One primitive, one render path (ponytail).
- **No `main.py` change** — Team construction is unchanged; the new callback is internal to `Team.supervisor()`.
- **Scheduler is concurrency-safe already** (verify, don't change): `AdmissionControl` uses a real `asyncio.Condition`; AIMD/breaker/budget mutate only in await-free sections. The benign RPM TOCTOU across concurrent `enter_turn` is acceptable (note with `# ponytail:`, don't fix).
- **Results aggregate in input order** (`zip(delegations, results)`) — `asyncio.gather` preserves order regardless of completion order.

---

## Chunk 1: Backend primitive + provider tool surface + tests (TDD — one green commit)

This chunk adds new behavior, so it is **test-first**: rewrite the orchestrator tests for `delegate_many` (red), then implement (green). The orchestrator + provider changes are mutually dependent (`supervisor()` passes the new callback), so they land in one commit.

### Task 1: Rewrite `tests/test_orchestrator.py` for `delegate_many` (red)

**Files:** Modify `tests/test_orchestrator.py`; extend `tests/fakes.py`.

- [ ] **Step 1: Add `FakeBlockingProvider` to `tests/fakes.py`** — a Provider that counts concurrent entries and parks on an `asyncio.Event` before yielding. Proves >1 worker is in-flight simultaneously (sequential dispatch deadlocks at 1).

```python
class FakeBlockingProvider:
    """Counts concurrent entries and parks on `release` before yielding.

    For delegate_many concurrency proof: the entry counter reaches N only if N
    workers were dispatched concurrently. Sequential dispatch parks worker 1 on
    `release` and never starts worker 2, so the counter stalls at 1 → test times out.
    """
    def __init__(self, events, counter, release):
        self._events = events
        self._counter = counter  # list[int] — mutable holder, counter[0]
        self._release = release  # asyncio.Event

    async def stream(self, prompt, *, system_prompt, allowed_tools, session_id, on_tool_use):
        self._counter[0] += 1
        await self._release.wait()
        for ev in self._events:
            yield ev
```

- [ ] **Step 2: Rewrite the existing tests** — every `team.delegate(role, task)` becomes `team.delegate_many([{"role": role, "task": task}])`; results are now wrapped in `### {role}\n…`, so assert with `in`:

  - `test_delegate_many_returns_worker_final_text` — `assert "drafted the module" in out`.
  - `test_delegate_many_unknown_role_returns_error_string_listing_roles` — `assert "Unknown role" in out and "coder" in out`.
  - `test_delegate_many_worker_over_budget_returns_error_string` — `assert "failed" in out.lower() and "budget" in out.lower()`.
  - `test_delegate_many_releases_admission_slot` — after a 1-item fan-out, `sched.admission.in_flight == 0`.
  - `test_delegate_many_records_worker_tokens_on_scheduler` — `sched.rate_tracker.token_history` non-empty.
  - `test_workers_are_leaves_no_delegate_many_tool` — `assert "delegate_many" not in role.allowed_tools and "delegate" not in role.allowed_tools` and `"Read" in role.allowed_tools`.
  - `test_supervisor_session_has_delegate_many_tool` — `assert "delegate_many" in sup.allowed_tools` and `sup.session_id.startswith("supervisor-")`.
  - `test_delegate_many_forwards_worker_events_with_fanout_id` — drain `sup.events`; forwarded `worker_event` frames carry `role`, `worker_id.startswith("worker-coder-")`, and a single shared `fanout_id.startswith("fanout-")`; nested `stream_end` stays nested (`all(e.get("type") != "stream_end")`); persisted in `sup.messages`.
  - `test_delegate_many_registers_worker_so_approval_is_actionable` — single-item fan-out with `FakeGatedProvider`; wait for the worker + its pending approval, resolve via `worker.approve_tool(id, True)`, `assert "wrote it" in out`, `gated.verdict.allow is True`, registered but hidden from `list_sessions()`.

- [ ] **Step 3: Add the two new tests:**

```python
def test_delegate_many_runs_workers_concurrently():
    """>1 worker in-flight at once proves concurrent dispatch (sequential deadlocks)."""
    async def go():
        counter = [0]
        release = asyncio.Event()
        team = Team(
            HiveMindScheduler(default_ceiling=10**9),
            worker_provider=FakeBlockingProvider(_worker_events("done"), counter, release),
        )
        team.hire(WorkerRole(name="coder", system_prompt="x"))
        fanout = asyncio.ensure_future(team.delegate_many([
            {"role": "coder", "task": "a"}, {"role": "coder", "task": "b"},
        ]))
        # Both enter (counter==2) only if dispatched concurrently.
        await _wait_for(lambda: counter[0] >= 2)
        release.set()
        out = await asyncio.wait_for(fanout, timeout=2.0)
        assert out.count("### coder") == 2
    asyncio.run(go())


def test_delegate_many_aggregates_per_role_results_in_input_order():
    async def go():
        team = Team(
            HiveMindScheduler(default_ceiling=10**9),
            worker_provider=FakeProvider(_worker_events("OUT")),
        )
        team.hire(WorkerRole(name="researcher", system_prompt="x"),
                  WorkerRole(name="coder", system_prompt="y"))
        out = await team.delegate_many([
            {"role": "researcher", "task": "r"}, {"role": "coder", "task": "c"},
        ])
        assert out.index("### researcher") < out.index("### coder")
    asyncio.run(go())
```

- [ ] **Step 4: Update the `__main__` self-check block** to call the new test names.

- [ ] **Step 5: Run — expect RED** (`AttributeError: 'Team' object has no attribute 'delegate_many'`, and `supervisor` still adds `"delegate"`).

Run: `.venv/bin/pytest tests/test_orchestrator.py -q`

### Task 2: Implement `delegate_many` + provider swap (green)

**Files:** Modify `app/core/orchestrator.py`, `app/core/provider.py`.

- [ ] **Step 1: `orchestrator.py` — add `import asyncio`; delete `delegate`; add `_run_worker` + `delegate_many`; rewrite `default_supervisor_prompt` + `supervisor()`.**

```python
async def _run_worker(self, role: str, task: str, fanout_id: str) -> str:
    """Run one worker for `role` on `task`; forward its events onto the
    supervisor's bus tagged with `fanout_id`. Never raises — failure / over-budget
    / crash returns a descriptive string (tool-result semantics). Extracted from
    the old delegate(); shared by every delegation in a delegate_many fan-out."""
    r = self.roles.get(role)
    if r is None:
        available = ", ".join(sorted(self.roles)) or "(none hired)"
        return f"Unknown role '{role}'. Available: {available}."

    worker = AgentSession(
        session_id=f"worker-{role}-{uuid.uuid4().hex[:8]}",
        prompt=task,
        scheduler=self.scheduler,
        system_prompt=r.system_prompt,
        security=self.security,
        provider=self.worker_provider,
        allowed_tools=r.allowed_tools,
        kind="worker",
    )

    sup = self._supervisor
    if sup is not None:
        role_name, worker_id = role, worker.session_id

        async def _to_supervisor(ev: dict) -> None:
            await sup.ingest({
                "type": "worker_event", "role": role_name,
                "worker_id": worker_id, "fanout_id": fanout_id, "event": ev,
            })

        worker.event_sink = _to_supervisor

    if self.register is not None:
        self.register(worker)

    logger.info("Supervisor fanning out to '%s': %.80s", role, task)
    try:
        await worker.run()
    except Exception as e:  # never raises — a crashed worker becomes a string
        logger.exception("Worker '%s' raised during run()", role)
        return f"Worker '{role}' crashed: {e}"

    if worker.status == "completed":
        return worker.final_text() or f"(worker '{role}' produced no text)"
    return f"Worker '{role}' ended {worker.status}: {worker.error_message or 'no detail'}"


async def delegate_many(self, delegations: list[dict]) -> str:
    """Fan out N workers concurrently; return per-role result sections in input
    order. Each worker is a first-class AgentSession through the shared
    scheduler/security/gate/receipts. _run_worker never raises, so gather cannot
    throw — a mixed success/failure fan-out returns all N sections.

    # ponytail: no explicit concurrency cap — AdmissionControl (AIMD live limit,
    # init 4 / max 10) already bounds in-flight workers and queues the rest.
    """
    if not delegations:
        return "(no delegations)"
    fanout_id = f"fanout-{uuid.uuid4().hex[:8]}"
    results = await asyncio.gather(*[
        self._run_worker(d.get("role", ""), d.get("task", ""), fanout_id)
        for d in delegations
    ])
    return "\n\n".join(f"### {d.get('role', '?')}\n{res}" for d, res in zip(delegations, results))
```

`default_supervisor_prompt` → instruct fan-out (`delegate_many`, `delegations: [{role, task}]`, fan out independent subtasks together, synthesize per-role results).

`supervisor()` → `tools.append("delegate_many")` and `provider=ClaudeSdkProvider(delegate_many=self.delegate_many)`.

- [ ] **Step 2: `provider.py` — swap the `delegate` ctor param + tool for `delegate_many`.**

`__init__` signature: `def __init__(self, delegate_many=None, mcp_servers=None):` storing `self._delegate_many`. In `stream()`, replace the `delegate` tool block:

```python
orbiter_server = None
if self._delegate_many is not None:
    async def _delegate_many(args: dict[str, Any]) -> dict[str, Any]:
        result = await self._delegate_many(args.get("delegations", []))
        return {"content": [{"type": "text", "text": result}]}

    delegate_many_tool = tool(
        "delegate_many",
        "Fan out subtasks to specialist workers concurrently. `delegations` is a "
        "list of {role, task}; each runs a worker in parallel. Returns per-role "
        "results under role headings.",
        {"delegations": [{"role": str, "task": str}]},
    )(_delegate_many)
    orbiter_server = create_sdk_mcp_server(name="orbiter", tools=[delegate_many_tool])
```

Update the `__init__` docstring + the `stream()` MCP comment to reference `delegate_many`. The `Provider` Protocol is **unchanged**.

- [ ] **Step 3: Run — expect GREEN.**

Run: `.venv/bin/pytest tests/test_orchestrator.py -q` → all green (the `delegate_many`/`_run_worker`/concurrency/aggregation tests pass).

- [ ] **Step 4: Lint changed files.**

Run: `.venv/bin/ruff check app/core/orchestrator.py app/core/provider.py tests/test_orchestrator.py tests/fakes.py`

- [ ] **Step 5: Commit.**

```bash
git restore --staged B-sessions/ 2>/dev/null || true   # runtime logs are not increments
git add app/core/orchestrator.py app/core/provider.py tests/test_orchestrator.py tests/fakes.py
git status   # confirm only this increment is staged
git commit -m "$(cat <<'EOF'
feat(orchestrator): delegate_many — concurrent fan-out (Centralized 2DOT route 2)

Replace Team.delegate (sequential) with delegate_many: asyncio.gather over N
workers, each a first-class AgentSession through the shared scheduler/security/
gate/receipts. Results aggregate as per-role sections in input order. The
worker_event frame gains fanout_id; the frontend groups lanes by it (next commit).
Provider swaps the delegate callback/tool for delegate_many (Protocol unchanged).
SDK-free tested incl. a concurrency-overlap proof (FakeBlockingProvider).

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Chunk 2: Frontend — grouped `fanout` block (one commit)

**Files:** Modify `web/src/types.ts`, `web/src/store.ts`, `web/src/components/Message.tsx`.

- [ ] **Step 1: `types.ts`** — add `fanout_id?: string;` to `StreamEvent` (worker_event group); add the `fanout` Row kind:

```typescript
| { kind: "fanout"; fanoutId: string; lanes: Extract<Row, { kind: "worker_lane" }>[] };
```

Keep the existing `worker_lane` member (it now renders only nested inside `fanout`).

- [ ] **Step 2: `store.ts` `applyEvent`** — rewrite the `worker_event` case to nest under a `fanout` container (find-or-create by `fanout_id`), then find-or-create the lane by `worker_id` inside it. The inner-event→lane logic (status / message / result / approval_needed) is unchanged, just operating on the nested lane. The standalone top-level `worker_lane` append is removed.

- [ ] **Step 3: `store.ts` `approveWorker`** — the selector now reaches lanes nested in `fanout`: map `r.kind === "fanout"` rows, clearing `l.approval` on the matching lane.

- [ ] **Step 4: `Message.tsx`** — add a `case "fanout"` rendering the grouped HUD block:

```tsx
case "fanout": {
  const live = row.lanes.filter((l) =>
    l.status === "running" || l.status === "pending_admission" || l.status === "waiting_approval").length;
  const done = row.lanes.length - live;
  return (
    <div className="row-in border border-edge-soft bg-void/60 px-3 py-2.5">
      <div className="mb-2 flex items-center justify-between border-b border-edge-soft pb-1.5">
        <span className="label"><span className="text-signal">▾ FAN-OUT</span> · {row.lanes.length} worker{row.lanes.length === 1 ? "" : "s"}</span>
        <span className="label text-faint">{live} live · {done} done</span>
      </div>
      <div className="space-y-2">
        {row.lanes.length === 0 && <div className="label text-faint">WORKERS SPINNING UP…</div>}
        {row.lanes.map((l, i) => <Message key={i} row={l} />)}
      </div>
    </div>
  );
}
```

The nested `<Message row={l} />` reuses the existing `worker_lane` case unchanged (header `◂ DELEGATED · role` + inner rows + `ApprovalCard`). No `Console.tsx` change (fanout is just another Row).

- [ ] **Step 5: Typecheck + build.**

Run: `cd web && npx tsc --noEmit && npm run build` → both green (`build` gates on tsc).

- [ ] **Step 6: Commit.**

```bash
git restore --staged B-sessions/ 2>/dev/null || true
git add web/src/types.ts web/src/store.ts web/src/components/Message.tsx
git status
git commit -m "$(cat <<'EOF'
feat(dashboard): grouped fan-out block for concurrent worker lanes

delegate_many's N concurrent workers render as one fanout Row (keyed by
fanout_id) holding stacked worker_lane rows — live/done counts derived from lane
statuses. applyEvent nests worker_events by fanout_id instead of appending at
top level; the standalone worker_lane path is removed (replace). Each lane
reuses the existing lane renderer + ApprovalCard.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Chunk 3: Verify + docs (one commit)

**Files:** Verify full suite; update `A-project/index.md`, `A-project/architecture.md`.

- [ ] **Step 1: Full backend suite + lint.**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check app/core tests` → all green (existing + new orchestrator tests; expect the test count to rise by ~1 net — rewrites + 2 new − removed `delegate`).

- [ ] **Step 2: Frontend gates.**

Run: `cd web && npx tsc --noEmit && npm run build` → green.

- [ ] **Step 3: `index.md`** — move `delegate_many` from "NEXT INCREMENT" to shipped in *Current status*: note `delegate` replaced by `delegate_many` (asyncio.gather fan-out), `worker_event` gains `fanout_id`, dashboard gains the grouped `fanout` block; the dedicated multi-lane view lands as the inline grouped block (panel still deferred). Name the new NEXT increment (suggestion: the array-schema integration check under z.ai/GLM, or Hierarchical topology). Link the ADR + this plan.

- [ ] **Step 4: `architecture.md`** — mark the Centralized-topology row with concurrent fan-out; one-line note on `delegate_many`.

- [ ] **Step 5: Commit docs.**

```bash
git restore --staged B-sessions/ 2>/dev/null || true
git add A-project/index.md A-project/architecture.md
git status
git commit -m "$(cat <<'EOF'
docs(index): delegate_many concurrent fan-out shipped — next increment

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Notes for the executor
- **Do not keep `delegate` around.** Replace means remove — the augment alternative was rejected. Leaving `delegate` is dead surface.
- **`fanout_id` always present on `worker_event` under replace.** The frontend trusts this; a missing `fanout_id` is a backend bug, not a render fallback.
- **`asyncio` import** in `orchestrator.py` is new — don't forget it.
- **The array-of-objects tool schema** (`{"delegations":[{"role":str,"task":str}]}`) is the SDK integration risk (harder for the GLM model to fill than today's flat `{role,task}`). The primitive is proven SDK-free regardless; the real-LLM fill is the deferred integration check noted in the ADR.
- **`B-sessions/` runtime logs** have been observed to land in the git index without `git add` — always `git restore --staged B-sessions/` and confirm `git status` shows only the increment before committing.
