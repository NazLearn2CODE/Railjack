"""Self-check for the PreToolUse approval gate.

The gate fires under z.ai (verified by a live spike); this checks the gate
LOGIC — emit/approve/deny/timeout — via the _on_tool_use/ToolDecision seam,
without a live query, via asyncio.run. The SDK-specific hook-output translation
is covered separately in test_provider.py.
Run: .venv/bin/python -m pytest tests/test_agent_approval.py -q
"""
import asyncio
import tempfile
from pathlib import Path

from app.core.agent import AgentSession
from app.core.provider import ToolDecision
from app.core.scheduler import HiveMindScheduler
from app.core.security import SecurityPolicy, WorkspaceBoundary, ShellPolicy, ToolReceiptLedger


def _make_session() -> AgentSession:
    return AgentSession("s-approval-test", "do a dangerous thing", HiveMindScheduler())


def _make_secured_session() -> AgentSession:
    """A session wired with a SecurityPolicy scoped to a throwaway workspace root."""
    log = Path(tempfile.mkdtemp()) / "r.jsonl"
    sec = SecurityPolicy(
        boundary=WorkspaceBoundary(roots=[Path(tempfile.mkdtemp())]),
        shell=ShellPolicy(),
        ledger=ToolReceiptLedger(secret="k", log_path=log),
    )
    return AgentSession("s-security-test", "do a catastrophic thing", HiveMindScheduler(), security=sec)


async def _allow_deny_scenario():
    s = _make_session()

    # --- approve -> allow ---
    task = asyncio.ensure_future(s._on_tool_use("Bash", {"command": "echo hi"}))
    await asyncio.sleep(0)  # let it register the future + emit
    assert s.status == "waiting_approval"
    approval_id = next(iter(s.pending_approvals))
    await s.approve_tool(approval_id, True)
    verdict = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(verdict, ToolDecision)
    assert verdict.allow is True
    assert s.status == "running"

    # --- deny -> deny ---
    task = asyncio.ensure_future(s._on_tool_use("Write", {"path": "/etc/x"}))
    await asyncio.sleep(0)
    approval_id = next(iter(s.pending_approvals))
    await s.approve_tool(approval_id, False)
    verdict = await asyncio.wait_for(task, timeout=2.0)
    assert verdict.allow is False
    assert verdict.reason  # deny carries a reason


async def _timeout_scenario():
    # A short-timeout session: prove the gate fails CLOSED on operator silence.
    s = _make_session()
    import app.core.agent as agent_mod
    orig = agent_mod.APPROVAL_TIMEOUT
    agent_mod.APPROVAL_TIMEOUT = 0.1  # patch the module constant the gate reads
    try:
        verdict = await s._on_tool_use("Edit", {})
        assert verdict.allow is False
        assert s.pending_approvals == {}, "timed-out approval must be cleaned up"
    finally:
        agent_mod.APPROVAL_TIMEOUT = orig


def test_approval_gate_allows_and_denies():
    asyncio.run(_allow_deny_scenario())


def test_approval_gate_denies_on_timeout():
    asyncio.run(_timeout_scenario())


async def _policy_short_circuit_scenario():
    # A catastrophic Bash command hard-denies at the policy floor — no approval card
    # is emitted, status never reaches waiting_approval, and approve_tool() is unused.
    s = _make_secured_session()
    verdict = await s._on_tool_use("Bash", {"command": "rm -rf /"})
    assert verdict.allow is False
    assert "security policy" in verdict.reason.lower()
    assert s.status != "waiting_approval", "catastrophic command must not request operator approval"
    assert s.events.empty(), "catastrophic command must not emit approval_needed"


def test_policy_floor_short_circuits_before_approval():
    asyncio.run(_policy_short_circuit_scenario())


if __name__ == "__main__":
    test_approval_gate_allows_and_denies()
    test_approval_gate_denies_on_timeout()
    test_policy_floor_short_circuits_before_approval()
    print("approval gate self-checks: OK")
