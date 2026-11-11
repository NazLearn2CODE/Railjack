"""Humanizer pass tests — masking/restore is pure and deterministic; the
LLM is stubbed. Live agy behavior is NOT under test here."""

import pytest
from fastapi import HTTPException

from app.newsroom.humanizer import (
    humanize_text,
    mask_structures,
    placeholders_ok,
    unmask,
)

SAMPLE = (
    "Bangkok Traffic Plan Announced\n"
    "\n"
    "**Chaiwathom [ชัยวัฒน์]** said the project starts ~~July 15, 2026~~ and "
    "costs 60,000 million baht. The **Department of Medical Services [กองบริการ]** "
    "agrees.\n"
    "\n"
    "It is a testament to the vibrant landscape of ~~next month~~."
)


def test_mask_captures_bold_underline_thai_headline():
    masked, table = mask_structures(SAMPLE)
    # headline line masked as one placeholder
    assert "[M0]" in masked
    # bold, underline, Thai runs all masked — none survive raw
    assert "**" not in masked
    assert "~~" not in masked
    assert "ชัยวัฒน์" not in masked
    # nothing lost: every original segment sits in the table
    assert "**Chaiwathom [ชัยวัฒน์]**" in table.values()
    assert "~~July 15, 2026~~" in table.values()


def test_roundtrip_is_byte_exact():
    masked, table = mask_structures(SAMPLE)
    assert unmask(masked, table) == SAMPLE


def test_placeholders_ok_detects_missing_and_extra():
    masked, table = mask_structures(SAMPLE)
    assert placeholders_ok(masked, table)
    assert not placeholders_ok(masked.replace("[M0]", ""), table)
    assert not placeholders_ok(masked + " [M99]", table)


def test_unmask_raises_on_stray_placeholder():
    masked, table = mask_structures(SAMPLE)
    bad = masked + " stray [M42]"
    with pytest.raises(KeyError):
        unmask(bad, table)


def test_short_text_skips_humanize(monkeypatch):
    async def fail(*a, **k):  # must never be called for short text
        raise AssertionError("LLM called for short text")
    monkeypatch.setattr("app.newsroom.humanizer._agy_complete", fail)
    assert humanize_text.__wrapped__ if hasattr(humanize_text, "__wrapped__") else True
    # direct await via asyncio
    import asyncio
    assert asyncio.run(humanize_text("too short")) == "too short"


def test_retry_then_502_on_unverifiable_output():
    import asyncio
    from app.newsroom import humanizer

    async def garbage(prompt, **k):
        return "rewritten text with no placeholders at all"

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(humanizer._agy_complete, "__module__", humanizer.__name__) if False else None
    monkeypatch.setattr(humanizer, "_agy_complete", garbage)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(humanizer.humanize_text(SAMPLE))
    assert ei.value.status_code == 502
    monkeypatch.undo()


def test_success_roundtrip_through_llm_stub():
    import asyncio
    from app.newsroom import humanizer

    async def passthrough(prompt, **k):
        masked = prompt.split("=== TEXT ===\n", 1)[1]
        # "humanized": pretend the model lightly edited around the placeholders
        return masked.replace("It is a testament to", "It shows")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(humanizer, "_agy_complete", passthrough)
    out = asyncio.run(humanizer.humanize_text(SAMPLE))
    monkeypatch.undo()
    assert "It shows" in out
    assert "testament to" not in out
    assert "**Chaiwathom [ชัยวัฒน์]**" in out       # markers restored byte-exact
    assert "~~July 15, 2026~~" in out
    assert out.startswith("Bangkok Traffic Plan Announced")  # headline intact


def test_percent_normalization_before_and_after_mask(monkeypatch):
    """British/UN 'per cent' is normalized to 'percent' on both sides of
    mask/unmask (headline, bold/underline spans, and model output)."""
    import asyncio
    from app.newsroom import humanizer

    src = (
        "Inflation Rises 5 Per Cent in April\n"
        "\n"
        "**Chaiwathom [ชัยวัฒน์]** said growth reached 4.2 per cent on ~~July 15, 2026~~ "
        "while exports climbed 12 PER CENT across the region during the quarter.\n"
        "\n"
        "Analysts expect another 3 per cent gain next quarter as trade recovers across "
        "Southeast Asian manufacturing hubs."
    )

    async def model_reintroduces_per_cent(prompt, **k):
        masked = prompt.split("=== TEXT ===\n", 1)[1]
        # Even if the model re-introduces "per cent" in body prose, exit normalization catches it
        return masked.replace("3 percent", "3 per cent")

    monkeypatch.setattr(humanizer, "_agy_complete", model_reintroduces_per_cent)
    out = asyncio.run(humanizer.humanize_text(src))
    assert "per cent" not in out.lower()
    assert "Inflation Rises 5 percent in April" in out
    assert "4.2 percent" in out
    assert "12 percent" in out
    assert "3 percent" in out

