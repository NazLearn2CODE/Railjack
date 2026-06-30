"""Self-check for the PreToolUse approval gate (task #8).

The hook fires under z.ai (verified by a live spike); this checks the gate
LOGIC — emit/approve/deny/timeout — without a live query, via asyncio.run.
Run: .venv/bin/python -m pytest tests/test_agent_approval.py -q
"""
import asyncio

from app.core.agent import AgentSession, APPROVAL_TIMEOUT
from app.core.scheduler import HiveMindScheduler


def _make_session() -> AgentSession:
    return AgentSession("s-approval-test", "do a dangerous thing", HiveMindScheduler())


async def _allow_deny_scenario():
    s = _make_session()

    # --- approve -> allow ---
    task = asyncio.ensure_future(
        s._pre_tool_use_hook({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, "tu-1", {"signal": None})
    )
    await asyncio.sleep(0)  # let it register the future + emit
    assert s.status == "waiting_approval"
    approval_id = next(iter(s.pending_approvals))
    await s.approve_tool(approval_id, True)
    out = await asyncio.wait_for(task, timeout=2.0)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow", out
    assert s.status == "running"

    # --- deny -> deny ---
    task = asyncio.ensure_future(
        s._pre_tool_use_hook({"tool_name": "Write", "tool_input": {"path": "/etc/x"}}, "tu-2", {"signal": None})
    )
    await asyncio.sleep(0)
    approval_id = next(iter(s.pending_approvals))
    await s.approve_tool(approval_id, False)
    out = await asyncio.wait_for(task, timeout=2.0)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    assert "permissionDecisionReason" in out["hookSpecificOutput"]


async def _timeout_scenario():
    # A short-timeout session: prove the gate fails CLOSED on operator silence.
    s = _make_session()
    import app.core.agent as agent_mod
    orig = agent_mod.APPROVAL_TIMEOUT
    agent_mod.APPROVAL_TIMEOUT = 0.1  # patch the module constant the hook reads
    try:
        out = await s._pre_tool_use_hook(
            {"tool_name": "Edit", "tool_input": {}}, "tu-3", {"signal": None}
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", out
        assert s.pending_approvals == {}, "timed-out approval must be cleaned up"
    finally:
        agent_mod.APPROVAL_TIMEOUT = orig


def test_approval_gate_allows_and_denies():
    asyncio.run(_allow_deny_scenario())


def test_approval_gate_denies_on_timeout():
    asyncio.run(_timeout_scenario())


if __name__ == "__main__":
    test_approval_gate_allows_and_denies()
    test_approval_gate_denies_on_timeout()
    print("approval gate self-checks: OK")
