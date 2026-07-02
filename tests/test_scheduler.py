"""Self-check for the scheduler primitives touched by task #6.

Uses asyncio.run inside plain sync tests so no pytest-asyncio is needed.
Run: .venv/bin/python -m pytest tests/test_scheduler.py -q
"""
import asyncio

from app.core.scheduler import AdmissionControl, AIMDController, TokenBudgetManager


def test_consume_enforces_ceiling_mid_turn():
    budget = TokenBudgetManager(default_ceiling=100)
    assert budget.consume("s1", 60) is True   # 60 < 100 -> under
    assert budget.consume("s1", 30) is True   # 90 < 100 -> under
    assert budget.consume("s1", 20) is False  # 110 >= 100 -> over -> caller breaks


def test_set_ceiling_overrides_default_for_one_key_only():
    """A team pool sets a per-key ceiling; other keys still get default_ceiling."""
    budget = TokenBudgetManager(default_ceiling=100)
    budget.set_ceiling("team-a", 250)
    assert budget.effective_ceiling("team-a") == 250
    assert budget.effective_ceiling("plain-session") == 100  # untouched
    # The override keys the accounting: 200 < 250 (under team pool) but would be
    # over the default ceiling — proves the override is honored, not the default.
    assert budget.consume("team-a", 200) is True
    assert budget.consume("team-a", 60) is False   # 260 >= 250 -> over
    # A different key is independently bounded by the default ceiling.
    assert budget.consume("team-b", 100) is False  # 100 >= 100 -> over (default)


async def _shrink_scenario():
    """Limit drops BELOW in-flight count: new acquires must block until a release."""
    aimd = AIMDController(initial_limit=3)
    ac = AdmissionControl(aimd)

    # Take all 3 slots.
    for _ in range(3):
        await ac.acquire()
    assert ac.in_flight == 3

    # Shrink the live limit to 1 while 3 are in flight.
    aimd.limit = 1

    started = {"flag": False}

    async def try_acquire():
        await ac.acquire()
        started["flag"] = True

    task = asyncio.ensure_future(try_acquire())
    await asyncio.sleep(0)  # let it schedule — it must block (in_flight 3 >= limit 1)
    assert started["flag"] is False, "acquire should block while in_flight >= limit"

    # Releasing one slot drops in_flight to 2, still >= 1 -> still blocked.
    await ac.release()
    await asyncio.sleep(0)
    assert started["flag"] is False

    # Second release drops in_flight to 1 == limit -> still blocked (>= not >).
    await ac.release()
    await asyncio.sleep(0)
    assert started["flag"] is False

    # Third release drops in_flight to 0 < 1 -> the waiter proceeds.
    await ac.release()
    await asyncio.wait_for(task, timeout=1.0)
    assert started["flag"] is True
    assert ac.in_flight == 1


def test_admission_shrink_below_in_flight_blocks_until_release():
    asyncio.run(_shrink_scenario())
