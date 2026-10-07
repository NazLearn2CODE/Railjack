"""SEO generation resilience — agy retry + glm budget escalation (2026-10-06).

The agy binary self-updates in place and briefly fails during the swap
(observed 10:56 2026-10-06), and glm-5.3-flash is a THINKING model that can
burn its whole budget reasoning on long documents (returns the literal
"(empty response)" placeholder at stop_reason=length). Contract:
  1. agy failure → ONE retry before falling to glm
  2. glm "(empty response)" → escalate once to a bigger budget
"""
import asyncio
import json

import pytest

from app.thailand_now import scout

VALID_SEO = (
    "# Focus Keyphrases\n1. chiang mai lantern festival\n2. yi peng 2026\n"
    "# Meta Descriptions\n1. The Yi Peng lantern festival returns this December.\n"
    "# Related Hashtags\n#ChiangMai #YiPeng\n"
    "# AI SEO Block\n# Version A\nsummary line for version a\n"
    "# Version B\nbullet one for version b\n* bullet two for version b\n"
)


def test_agy_down_retry_once_then_glm_fallback(monkeypatch):
    calls = {"agy": 0, "glm": 0, "glm_budgets": []}

    async def agy_fail(prompt, **kw):
        calls["agy"] += 1
        return None

    async def glm_ok(prompt, max_tokens=4096, system=None, model=None, timeout=None):
        calls["glm"] += 1
        calls["glm_budgets"].append(max_tokens)
        return VALID_SEO

    monkeypatch.setattr(scout, "_agy_complete", agy_fail)
    monkeypatch.setattr(scout, "zai_message", glm_ok)

    result, model = asyncio.run(scout._generate_event_seo("T", "B", "Events"))
    assert model == "glm-5.3-flash"
    assert calls["agy"] == 2, "agy must be retried exactly once before falling back"
    assert calls["glm"] == 1


def test_glm_empty_response_escalates_budget(monkeypatch):
    calls = {"n": 0, "budgets": []}

    async def agy_fail(prompt, **kw):
        return None

    async def glm_empty_then_ok(prompt, max_tokens=4096, system=None, model=None, timeout=None):
        calls["n"] += 1
        calls["budgets"].append(max_tokens)
        if calls["n"] == 1:
            return "(empty response)"
        return VALID_SEO

    monkeypatch.setattr(scout, "_agy_complete", agy_fail)
    monkeypatch.setattr(scout, "zai_message", glm_empty_then_ok)

    result, model = asyncio.run(scout._generate_event_seo("T", "B", "Events"))
    assert model == "glm-5.3-flash"
    assert calls["budgets"] == [8192, 16384], "must escalate budget on empty response"


def test_both_models_down_raises_503(monkeypatch):
    async def agy_fail(prompt, **kw):
        return None

    async def glm_fail(prompt, **kw):
        return None

    monkeypatch.setattr(scout, "_agy_complete", agy_fail)
    monkeypatch.setattr(scout, "zai_message", glm_fail)

    with pytest.raises(Exception) as exc:
        asyncio.run(scout._generate_event_seo("T", "B", "Events"))
    assert "both failed" in str(exc.value)
