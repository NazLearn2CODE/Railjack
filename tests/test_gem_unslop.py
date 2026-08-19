"""Lint: writing gems must carry the anti-slop clause and its carve-outs.

Anchor feedback 2026-08-19: "adj after adj after adj coupled with long sentences"
in AI-written scripts. The clause lives in the gems (runtime system prompts);
this guards against gem drift silently losing it. Decision record:
Cephalon 30-decisions/2026-08-19-unslop-integration.md.
"""
from pathlib import Path

GEMS = Path(__file__).resolve().parent.parent / "app" / "gems"


def _gem(name: str) -> str:
    return (GEMS / name).read_text(encoding="utf-8")


def test_ben_gem_has_antislop_rule():
    g = _gem("radio-news-rewrite.md")
    assert "No adjective stacking" in g
    assert "ONE adjective per noun" in g
    assert "25 words" in g
    assert "No em dashes" in g


def test_ben_gem_protects_name_overlay_and_signposting():
    g = _gem("radio-news-rewrite.md")
    # The overlay markers are functional formatting; unslop must not strip them.
    assert "**[OfficialEnglish(Thai)]**" in g
    assert "rule 12" in g  # carve-out references the overlay rule
    # Signposting connectives are structural, exempt from the adverb cut.
    assert "structural, not adverbs" in g


def test_ben_gem_examples_not_stacked():
    g = _gem("radio-news-rewrite.md")
    # The old example ledes modelled the exact slop rule 14 bans.
    assert "most striking" not in g
    assert "massive new deal" not in g
    assert "freshly paved" not in g


def test_pitch_gem_has_antislop_block():
    g = _gem("story-scout-pitch.md")
    assert "no AI slop" in g
    assert "ONE adjective per noun" in g
    assert "not just X but Y" in g


def test_publicity_gem_language_subset_keeps_promo_register():
    g = _gem("event-publicity.md")
    assert "No AI filler" in g
    # Promotional adjectives are deliberately allowed in publicity copy.
    assert "Promotional adjectives are this copy's register and stay" in g
