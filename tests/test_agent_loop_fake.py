"""SDK-free agent-loop tests via FakeProvider.

Exercises the run() loop (status transitions, token accounting, mid-turn budget
enforcement) with no CLI/network — the payoff of the Provider abstraction.
Run: .venv/bin/python -m pytest tests/test_agent_loop_fake.py -q
"""
import asyncio

from app.core.agent import AgentSession
from app.core.scheduler import HiveMindScheduler
from tests.fakes import FakeProvider


def test_fake_session_completes_and_counts_tokens():
    async def go():
        s = AgentSession(
            "s-fake",
            "hi",
            HiveMindScheduler(default_ceiling=10**9),
            provider=FakeProvider([
                {"type": "result", "result": "ok", "is_error": False,
                 "usage": {"input_tokens": 100, "output_tokens": 5}},
            ]),
        )
        await s.run()
        assert s.status == "completed"
        assert s.tokens_consumed == 105

    asyncio.run(go())


def test_fake_session_stops_on_budget_breach():
    async def go():
        s = AgentSession(
            "s-budget",
            "hi",
            HiveMindScheduler(default_ceiling=50),
            provider=FakeProvider([
                {"type": "message", "role": "assistant", "content": [], "uuid": "u1",
                 "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                           "cache_creation_input_tokens": 0, "output_tokens": 0}},
                {"type": "result", "result": "never reached", "is_error": False, "usage": {}},
            ]),
        )
        await s.run()
        assert s.status == "failed"
        assert "budget" in (s.error_message or "").lower()

    asyncio.run(go())


if __name__ == "__main__":
    test_fake_session_completes_and_counts_tokens()
    test_fake_session_stops_on_budget_breach()
    print("fake agent-loop self-checks: OK")
