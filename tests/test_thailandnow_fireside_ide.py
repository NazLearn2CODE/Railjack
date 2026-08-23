"""FIRESIDE IDE lane — /scout/fireside/convert handoff reader + ide-prompt builder.

Covers what the lane added (home ref: Railjack, 2026-08-23; pattern =
[[thailandnow-events-antigravity-handoff]], implement-not-copy from Tasai's
Somatic IDE lane):
  * ``_fireside_topic_row`` — coercion into the FiresideTopic card shape;
  * ``fireside_ide_convert`` — soft-fail on missing/bad handoff, dedup on title
    slug, ``covered`` flagging against the done/excluded registry;
  * ``fireside_ide_prompt`` — done-list inlined from the registry, degrades to
    fixed avoids when the registry is unreachable.

No HTTP client: async handlers driven via ``asyncio.run``; the handoff path and
the registry fetcher are steered by monkeypatching module constants (same
pattern as test_thailandnow_events.py)."""
import asyncio
import json

from app import thailandnow
from app.thailandnow import _fireside_topic_row, fireside_ide_convert, fireside_ide_prompt


def test_topic_row_coercion_and_rejection():
    row = _fireside_topic_row({
        "title": "  Visa Run Endgame  ", "angle": "What changes? Who's hit?",
        "ep_adjacent": ["EP12 Visa Amnesty"], "source_urls": "https://example.com/one",
        "if_like_a_try_b": "If you liked EP12…", "visual_style": "map + lower-third",
        "why_fresh": "police order takes effect Sep 1", "revisit_candidate": True,
    })
    assert row["title"] == "Visa Run Endgame"
    assert row["source_urls"] == ["https://example.com/one"]  # single string tolerated
    assert row["revisit_candidate"] is True
    assert _fireside_topic_row({"title": "   "}) is None      # no title → unusable
    assert _fireside_topic_row({"angle": "x"}) is None


def _fake_registry(rows):
    async def fake():
        return rows
    return fake


HANDOFF_ROW = {
    "title": "Visa Run Endgame",
    "angle": "What changes? Who's hit?",
    "source_urls": ["https://example.com/one", "https://example.com/two"],
    "why_fresh": "police order takes effect Sep 1",
}


def test_convert_missing_file_soft_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(thailandnow, "_FIRESIDE_IDE_HANDOFF", tmp_path / "nope.json")
    out = asyncio.run(fireside_ide_convert())
    assert out["topics"] == [] and out["count"] == 0
    assert "IDE SOURCE" in out["errors"][0]


def test_convert_bad_json_soft_fails(monkeypatch, tmp_path):
    p = tmp_path / "latest.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_FIRESIDE_IDE_HANDOFF", p)
    out = asyncio.run(fireside_ide_convert())
    assert out["topics"] == [] and "valid JSON" in out["errors"][0]


def test_convert_dedup_and_covered_flags(monkeypatch, tmp_path):
    p = tmp_path / "latest.json"
    # dict envelope + a duplicate (same slug) + a title-less junk row
    p.write_text(json.dumps({"topics": [
        HANDOFF_ROW,
        {**HANDOFF_ROW, "title": "Visa Run  Endgame!", "angle": "dupe slug"},
        {"angle": "no title"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_FIRESIDE_IDE_HANDOFF", p)
    registry = [
        {"topic": "Visa Run Endgame", "status": "done"},
        {"topic": "Old Night Market Tour", "status": "excluded"},
        {"topic": "Trains Revival", "status": "revisitable"},
    ]
    monkeypatch.setattr(thailandnow, "_fireside_registry", _fake_registry(registry))
    out = asyncio.run(fireside_ide_convert())
    assert out["count"] == 1                     # dedup collapsed the slug twin
    assert out["covered"] == 1
    assert out["topics"][0]["covered"] is True   # slug matches the done registry row
    assert out["topics"][0]["source_urls"] == ["https://example.com/one", "https://example.com/two"]


def test_convert_registry_down_still_converts(monkeypatch, tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps([HANDOFF_ROW]), encoding="utf-8")
    monkeypatch.setattr(thailandnow, "_FIRESIDE_IDE_HANDOFF", p)

    async def boom():
        raise RuntimeError("sheets down")
    monkeypatch.setattr(thailandnow, "_fireside_registry", boom)
    out = asyncio.run(fireside_ide_convert())
    assert out["count"] == 1 and out["covered"] == 0
    assert out["topics"][0]["covered"] is False  # no flag, topics still convert


def test_ide_prompt_inlines_done_list(monkeypatch):
    registry = [{"topic": "Visa Run Endgame: finale", "status": "done"}]
    monkeypatch.setattr(thailandnow, "_fireside_registry", _fake_registry(registry))
    out = asyncio.run(fireside_ide_prompt(seed="soft power"))
    assert "fireside-ide-handoff.md" in out["text"]
    assert "soft power" in out["text"]
    assert "Visa Run Endgame" in out["text"]       # colon-suffix stripped, inlined
    assert "Queen-related" in out["text"]           # fixed avoid always present
    assert out["done_count"] == 1


def test_ide_prompt_degrades_without_registry(monkeypatch):
    async def boom():
        raise RuntimeError("sheets down")
    monkeypatch.setattr(thailandnow, "_fireside_registry", boom)
    out = asyncio.run(fireside_ide_prompt())
    assert "registry unreadable" in out["text"]
    assert out["done_count"] == 0
