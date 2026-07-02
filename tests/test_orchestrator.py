"""SDK-free Centralized-topology tests via FakeProvider.

Team.delegate_many() is the OS primitive: it fans out N workers concurrently
(asyncio.gather), each a first-class AgentSession through the shared
scheduler/security, and returns per-role result sections in input order.
FakeProvider / FakeBlockingProvider drive workers deterministically — no CLI, no
network. The supervisor-side MCP wiring (ClaudeSdkProvider(delegate_many=...))
is the SDK/LLM integration boundary and is exercised only in the real path; see
ADR 2026-07-02-delegate-many-fanout.

Run: .venv/bin/python -m pytest tests/test_orchestrator.py -q
"""
import asyncio

from app.core.orchestrator import Team, WorkerRole
from app.core.scheduler import HiveMindScheduler
from tests.fakes import FakeProvider, FakeBlockingProvider


def _worker_events(text: str = "worker result") -> list[dict]:
    return [
        {"type": "message", "role": "assistant",
         "content": [{"type": "text", "text": text}], "uuid": "u1", "usage": None},
        {"type": "result", "result": text, "is_error": False, "usage": {}},
    ]


async def _wait_for(pred, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for condition")


def test_delegate_many_returns_worker_final_text():
    async def go():
        team = Team(
            HiveMindScheduler(default_ceiling=10**9),
            worker_provider=FakeProvider(_worker_events("drafted the module")),
        )
        team.hire(WorkerRole(name="coder", system_prompt="you write code"))
        out = await team.delegate_many([{"role": "coder", "task": "write a function"}])
        assert "drafted the module" in out

    asyncio.run(go())


def test_delegate_many_unknown_role_returns_error_string_listing_roles():
    async def go():
        team = Team(HiveMindScheduler(), worker_provider=FakeProvider([]))
        team.hire(WorkerRole(name="coder", system_prompt="x"))
        out = await team.delegate_many([{"role": "ghost", "task": "x"}])
        assert "Unknown role" in out
        assert "coder" in out  # available roles are listed

    asyncio.run(go())


def test_delegate_many_worker_over_budget_returns_error_string():
    async def go():
        team = Team(
            HiveMindScheduler(default_ceiling=50),
            worker_provider=FakeProvider([
                {"type": "message", "role": "assistant", "content": [], "uuid": "u1",
                 "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0, "output_tokens": 0}},
                {"type": "result", "result": "x", "is_error": False, "usage": {}},
            ]),
        )
        team.hire(WorkerRole(name="coder", system_prompt="x"))
        out = await team.delegate_many([{"role": "coder", "task": "x"}])
        assert "failed" in out.lower()
        assert "budget" in out.lower()

    asyncio.run(go())


def test_delegate_many_releases_admission_slot():
    """A completed fan-out returns every worker's concurrency slot to the scheduler."""
    async def go():
        sched = HiveMindScheduler()
        team = Team(sched, worker_provider=FakeProvider(_worker_events("done")))
        team.hire(WorkerRole(name="coder", system_prompt="x"))
        await team.delegate_many([{"role": "coder", "task": "x"}])
        assert sched.admission.in_flight == 0

    asyncio.run(go())


def test_delegate_many_records_worker_tokens_on_scheduler():
    """Worker throughput feeds the shared scheduler's rate-limit (TPM) window."""
    async def go():
        sched = HiveMindScheduler()
        team = Team(sched, worker_provider=FakeProvider([
            {"type": "message", "role": "assistant", "content": [], "uuid": "u1",
             "usage": {"input_tokens": 70, "cache_read_input_tokens": 0,
                       "cache_creation_input_tokens": 0, "output_tokens": 30}},
            {"type": "result", "result": "done", "is_error": False,
             "usage": {"input_tokens": 70, "output_tokens": 30}},
        ]))
        team.hire(WorkerRole(name="coder", system_prompt="x"))
        await team.delegate_many([{"role": "coder", "task": "x"}])
        # exit_turn feeds actual_tokens (the result usage throughput) into the TPM window.
        assert sched.rate_tracker.token_history  # non-empty

    asyncio.run(go())


def test_workers_are_leaves_no_delegate_many_tool():
    """Recursion guard: a role's default tool set excludes delegation primitives."""
    role = WorkerRole(name="coder", system_prompt="x")
    assert "delegate_many" not in role.allowed_tools
    assert "delegate" not in role.allowed_tools
    assert "Read" in role.allowed_tools  # native tools still present


def test_supervisor_session_has_delegate_many_tool():
    """The supervisor's allowed_tools includes 'delegate_many'; it is a normal AgentSession."""
    team = Team(HiveMindScheduler(), worker_provider=FakeProvider([]))
    sup = team.supervisor("plan the work", system_prompt="you delegate")
    assert "delegate_many" in sup.allowed_tools
    assert sup.session_id.startswith("supervisor-")


def test_delegate_many_forwards_worker_events_with_fanout_id():
    """Worker activity streams onto the supervisor's bus as nested worker_event
    frames (live observability), each tagged with a shared fanout_id (the grouping
    key), and is persisted in supervisor.messages (replay)."""
    async def go():
        team = Team(
            HiveMindScheduler(default_ceiling=10**9),
            worker_provider=FakeProvider(_worker_events("drafted the module")),
        )
        team.hire(WorkerRole(name="coder", system_prompt="x"))
        sup = team.supervisor("plan the work")  # sets the forward target
        out = await team.delegate_many([{"role": "coder", "task": "write a function"}])
        assert "drafted the module" in out

        drained = []
        while not sup.events.empty():
            drained.append(sup.events.get_nowait())
        we = [e for e in drained if e.get("type") == "worker_event"]
        assert we, "expected worker events forwarded to the supervisor bus"
        # Every forwarded frame is tagged with role + worker id + a shared fanout id.
        assert all(e["role"] == "coder" and e["worker_id"].startswith("worker-coder-") for e in we)
        ids = {e["fanout_id"] for e in we}
        assert len(ids) == 1 and ids.pop().startswith("fanout-")
        inner_types = {e["event"]["type"] for e in we}
        assert "message" in inner_types and "result" in inner_types
        # The nested stream_end stays nested → it must NOT terminate the supervisor's WS loop.
        assert all(e.get("type") != "stream_end" for e in drained)
        # Persisted for replay via GET /api/sessions/{supervisor}.
        assert any(m.get("type") == "worker_event" for m in sup.messages)

    asyncio.run(go())


def test_delegate_many_runs_workers_concurrently():
    """>1 worker in-flight at once proves concurrent dispatch. Under sequential
    dispatch worker 1 parks on `release` and worker 2 never starts, so the wait
    for counter>=2 times out (the red state)."""
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
        await _wait_for(lambda: counter[0] >= 2)  # both in-flight — impossible if sequential
        release.set()
        out = await asyncio.wait_for(fanout, timeout=2.0)
        assert out.count("### coder") == 2  # two sections, in input order

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


def _sole_worker(manager):
    ws = [s for s in manager.sessions.values() if s.kind == "worker"]
    return ws[0] if ws else None


def _sole_pending_id(session):
    ids = list(session.pending_approvals)
    return ids[0] if ids else None


def test_delegate_many_registers_worker_so_approval_is_actionable():
    """A worker's gated tool surfaces as a registered, approvable session — no
    longer a 600s fail-closed block. The operator resolves it through the same
    approve_tool() the REST /approve endpoint calls; list_sessions hides workers."""
    from app.core.agent import AgentSessionManager
    from tests.fakes import FakeGatedProvider

    async def go():
        manager = AgentSessionManager(HiveMindScheduler(default_ceiling=10**9))
        gated = FakeGatedProvider(
            "Write", {"path": "/tmp/worker-out", "content": "x"}, _worker_events("wrote it")
        )
        team = Team(manager.scheduler, worker_provider=gated, register=manager.register)
        team.hire(WorkerRole(name="coder", system_prompt="x"))

        delegate_task = asyncio.ensure_future(
            team.delegate_many([{"role": "coder", "task": "write a fn"}])
        )

        worker = await _wait_for(lambda: _sole_worker(manager))
        assert worker.kind == "worker"
        approval_id = await _wait_for(lambda: _sole_pending_id(worker))

        # Shared operator path — POST /api/sessions/{id}/approve calls this method.
        await worker.approve_tool(approval_id, True)

        out = await asyncio.wait_for(delegate_task, timeout=2.0)
        assert "wrote it" in out
        assert gated.verdict is not None and gated.verdict.allow is True
        # Registered (approvable) but kept out of the sidebar list.
        assert manager.get_session(worker.session_id) is worker
        assert all(s["kind"] != "worker" for s in manager.list_sessions())

    asyncio.run(go())


if __name__ == "__main__":
    test_delegate_many_returns_worker_final_text()
    test_delegate_many_unknown_role_returns_error_string_listing_roles()
    test_delegate_many_worker_over_budget_returns_error_string()
    test_delegate_many_releases_admission_slot()
    test_delegate_many_records_worker_tokens_on_scheduler()
    test_workers_are_leaves_no_delegate_many_tool()
    test_supervisor_session_has_delegate_many_tool()
    test_delegate_many_forwards_worker_events_with_fanout_id()
    test_delegate_many_runs_workers_concurrently()
    test_delegate_many_aggregates_per_role_results_in_input_order()
    test_delegate_many_registers_worker_so_approval_is_actionable()
    print("orchestrator self-checks: OK")
