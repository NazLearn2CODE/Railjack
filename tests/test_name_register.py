"""＋wiki register loop — endpoint + checker english-hint tests.

Railjack-native port of Somatic ``96c2296`` (2026-09-01): sync tests +
``asyncio.run`` (Railjack shape), monkeypatched ``newsroom._NAME_WIKI``.

The endpoint writes REAL vault notes, so every endpoint test points
``_NAME_WIKI`` at a tmp dir. The checker tests point the registry at a
missing dir so they never depend on the live vault (that adds a
``registry`` warning first — filtered).
"""
import asyncio
from pathlib import Path

from fastapi import HTTPException

from app import newsroom
from app.name_check import check_rewritten

PAIR = {"english": "Probe Testname", "thai": "โพรบ เทสต์เนม", "kind": "person"}
NO_REG = Path("/nonexistent-name-wiki")


def _unverified(res):
    return [w for w in res["warnings"] if w["kind"] == "unverified"]


def test_register_happy_path_writes_template_note(tmp_path, monkeypatch):
    monkeypatch.setattr(newsroom, "_NAME_WIKI", tmp_path)

    async def run():
        return await newsroom.namecheck_register(
            {**PAIR, "source": "https://example.test/x"}
        )

    res = asyncio.run(run())
    assert res["ok"] is True
    note = tmp_path / "probe-testname.md"
    assert note.exists()
    text = note.read_text(encoding="utf-8")
    assert 'english: "Probe Testname"' in text
    assert 'thai: "โพรบ เทสต์เนม"' in text
    assert "kind: person" in text
    assert 'source: "https://example.test/x"' in text
    # the registry loader (what the checker reads) must see it immediately
    reg, err = newsroom.load_registry(tmp_path)
    assert err is None and reg["โพรบ เทสต์เนม"] == "Probe Testname"


def test_register_duplicate_thai_key_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(newsroom, "_NAME_WIKI", tmp_path)

    async def run():
        first = await newsroom.namecheck_register(dict(PAIR))
        second = await newsroom.namecheck_register(
            {"english": "Other Spelling", "thai": PAIR["thai"]}
        )
        return first, second

    first, second = asyncio.run(run())
    assert first["ok"] is True
    assert second["ok"] is False and "already registered" in second["reason"]
    assert not (tmp_path / "other-spelling.md").exists()


def test_register_refuses_title_carrying_thai(tmp_path, monkeypatch):
    monkeypatch.setattr(newsroom, "_NAME_WIKI", tmp_path)

    async def run():
        return await newsroom.namecheck_register(
            {"english": "Some Minister", "thai": "นาย โพรบ"}
        )

    res = asyncio.run(run())
    assert res["ok"] is False and "title/rank" in res["reason"]


def test_register_slug_collision_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(newsroom, "_NAME_WIKI", tmp_path)

    async def run():
        first = await newsroom.namecheck_register(dict(PAIR))
        second = await newsroom.namecheck_register(
            {"english": "Probe   Testname!", "thai": "อื่น ๆ"}  # same slug, other thai
        )
        return first, second

    first, second = asyncio.run(run())
    assert first["ok"] is True
    assert second["ok"] is False and "already exists" in second["reason"]


def test_register_validates_input(tmp_path, monkeypatch):
    monkeypatch.setattr(newsroom, "_NAME_WIKI", tmp_path)

    async def run():
        raises = []
        for body in (
            {"thai": "โพรบ"},  # no english
            {"english": "X", "thai": "no thai"},  # no Thai
            {"english": "X", "thai": "โพรบ", "kind": "alien"},  # bad kind
            {"english": "แค่ไทย", "thai": "โพรบ"},  # non-Latin english
        ):
            try:
                await newsroom.namecheck_register(body)
            except HTTPException:
                raises.append(body)
        return raises

    assert len(asyncio.run(run())) == 4


def test_checker_english_hint_rides_unverified_warning():
    blob = (
        "EN: t\nTH: ท\n\n"
        "**Phattrapong Phattraprasit [ภัทรพงศ์ ภัทรประสิทธิ์]** worked."
    )
    res = check_rewritten(blob, NO_REG)
    (w,) = _unverified(res)
    assert w["english"] == "Phattrapong Phattraprasit"
    assert w["name"] == "ภัทรพงศ์ ภัทรประสิทธิ์"


def test_checker_english_hint_none_for_bare_bracket():
    res = check_rewritten("EN: t\nTH: ท\n\nThe bare **[สุรชัย หนูพรหม]** zone.", NO_REG)
    (w,) = _unverified(res)
    assert w["english"] is None
