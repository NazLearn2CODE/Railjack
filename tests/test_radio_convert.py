"""Tests for ``app.media.radio_convert`` — Convert/Apply split (Naz 2026-11-05):
- explicit ``split_title_body`` + title lint + enforced demotion (>16w)
- durable per-URL convert cache under ``~/.config/railjack/radio_converted``
- ``convert_piece`` pipeline order (4-pass -> humanize -> figure-verify -> split)
- ``plan_placement`` (preview == write contract) with doc-verified dedup
- ``apply_converted`` hard gate + verbatim ``fill --explicit`` write
"""

import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.media import radio_convert as rc


# ---------------------------------------------------------------- split_title_body + lint


def test_split_title_body_clean_headline():
    text = (
        "Japan Lifts Rate to Twenty-Year High\n\n"
        "The Bank of Japan raised its benchmark interest rate to 0.75 percent.\n\n"
        "Markets reacted calmly across the region."
    )
    title, body, problems = rc.split_title_body(text, fallback_title="BOJ hike")
    assert title == "Japan Lifts Rate to Twenty-Year High"
    assert "The Bank of Japan raised" in body
    assert "Japan Lifts Rate" not in body
    assert problems == []


def test_split_title_body_strips_headline_prefix_and_flags():
    text = (
        "HEADLINE: Fed Holds Rates Steady in March\n\n"
        "The Federal Reserve kept rates unchanged."
    )
    title, body, problems = rc.split_title_body(text)
    assert title == "Fed Holds Rates Steady in March"
    assert body == "The Federal Reserve kept rates unchanged."
    assert any("prefix" in p for p in problems)


def test_split_title_body_demotes_long_lede_and_uses_fallback():
    """Bug #3 fix: when the LLM starts directly with the lede sentence (>16
    words) instead of a short headline, line 1 is demoted into the body and
    fallback_title becomes the headline so a paragraph never lands in the H2 slot."""
    lede = (
        "Across the archipelago on Monday morning, Indonesian authorities arrested "
        "twenty-four suspects across three provinces following a coordinated "
        "counter-terrorism sweep over the weekend."
    )
    para2 = "Police said the group had been planning attacks on logistics hubs."
    text = f"{lede}\n\n{para2}"

    title, body, problems = rc.split_title_body(
        text, fallback_title="Indonesia Arrests 24 in Counter-Terror Sweep"
    )
    assert title == "Indonesia Arrests 24 in Counter-Terror Sweep"
    assert lede in body
    assert para2 in body
    assert any("long" in p for p in problems)
    assert any("demoted" in p for p in problems)


def test_split_title_body_single_paragraph_uses_fallback():
    text = "Short single paragraph body with no blank line separator."
    title, body, problems = rc.split_title_body(text, fallback_title="Scout Title")
    assert title == "Scout Title"
    assert body == text
    assert any("single-block" in p for p in problems)


# ---------------------------------------------------------------- cache roundtrip


def test_cache_put_and_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    url = "https://example.com/story-1"
    assert rc.cache_get(url) is None

    entry = {
        "url": url,
        "title": "Clean Headline",
        "body": "Paragraph one.\n\nParagraph two.",
        "words": 4,
    }
    rc.cache_put(entry)
    got = rc.cache_get(url)
    assert got is not None
    assert got["title"] == "Clean Headline"
    assert got["body"] == "Paragraph one.\n\nParagraph two."


# ---------------------------------------------------------------- convert_piece pipeline


def test_convert_piece_runs_all_passes_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")

    order: list[str] = []

    async def fake_pass1(source, model=None):
        order.append("pass1")
        return "Rough translation of the story with 12 percent growth."

    async def fake_zai(prompt, max_tokens=1500, system=None, model=None, timeout=120.0):
        order.append("pass3")
        return (
            "Exports Jump Twelve Percent in April\n\n"
            "Regional exports rose 12 percent in April as electronics demand recovered.\n\n"
            "Shipments to major ports climbed steadily through the quarter."
        )

    async def fake_humanize(text):
        order.append("humanize")
        return text + " [H]"

    def fake_verify(source, rewritten):
        order.append("verify")
        return {"ok": True, "checked": 1, "mismatches": []}

    monkeypatch.setattr(rc, "pass1_rough", fake_pass1)
    monkeypatch.setattr(rc.zai, "zai_message", fake_zai)
    monkeypatch.setattr(rc, "humanize", fake_humanize)
    monkeypatch.setattr(rc, "verify_figures", fake_verify)
    monkeypatch.setattr(rc, "verify_block", lambda vf: "")
    monkeypatch.setattr(rc, "freshness_scan", lambda _t: [])
    monkeypatch.setattr(
        rc,
        "mech_checks",
        lambda _t: {"namecheck": {"ok": True}, "stylecheck": {"ok": True}},
    )

    piece = {
        "url": "https://example.com/exports",
        "title": "Exports Jump in April",
        "content": "A" * 120 + " 12 percent growth in April across regional ports.",
        "region": "SEA",
    }
    entry = asyncio.run(rc.convert_piece(piece))
    assert order == ["pass1", "pass3", "humanize", "verify"]
    assert entry["title"] == "Exports Jump Twelve Percent in April"
    assert "[H]" in entry["body"]
    assert entry["title"] not in entry["body"]
    assert rc.cache_get("https://example.com/exports") is not None


# ---------------------------------------------------------------- placement + apply


def test_plan_placement_uses_empty_slots_and_doc_verified_dedup(tmp_path, monkeypatch):
    """Dedup is doc-verified (checks whether the headline actually appears in
    the live Google Doc text), never ledger-blind."""
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")

    rc.cache_put({
        "url": "https://example.com/1",
        "title": "Already In Live Doc Headline",
        "body": "Body 1",
        "words": 10,
    })
    rc.cache_put({
        "url": "https://example.com/2",
        "title": "Brand New Story Headline",
        "body": "Body 2",
        "words": 20,
    })

    async def fake_run(argv, timeout=90, stdin=None):
        cmd = argv[2]
        if cmd == "slots":
            return (
                0,
                json.dumps({
                    "slots": [
                        {"tab": "AM", "slot": 1, "filled": True, "title": "Already In Live Doc Headline"},
                        {"tab": "AM", "slot": 2, "filled": False, "title": ""},
                        {"tab": "MIDDAY", "slot": 1, "filled": False, "title": ""},
                    ]
                }).encode(),
                b"",
            )
        if cmd == "text":
            return (
                0,
                json.dumps({
                    "text": "=== AM ===\n1.[Already In Live Doc Headline]\nBody 1\n2.[]\n"
                }).encode(),
                b"",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(rc.radio_news, "_run", fake_run)

    plan = asyncio.run(
        rc.plan_placement(
            "DOC1",
            "weekday",
            "global",
            ["https://example.com/1", "https://example.com/2"],
        )
    )
    assert plan["slots_total"] == 3
    assert plan["slots_filled"] == 1
    assert plan["slots_empty"] == 2
    p0, p1 = plan["plan"]
    assert p0["action"] == "skip_in_doc"
    assert p1["action"] == "write"
    assert (p1["tab"], p1["slot"]) == ("AM", 2)


def test_plan_placement_does_not_skip_when_doc_is_actually_empty(tmp_path, monkeypatch):
    """Regression: even if a piece was converted earlier, if the live doc text
    does NOT contain its headline, plan_placement assigns it to ``write``."""
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")
    rc.cache_put({
        "url": "https://example.com/retry",
        "title": "Previously Failed Piece Headline",
        "body": "Body",
        "words": 15,
    })

    async def fake_run(argv, timeout=90, stdin=None):
        if argv[2] == "slots":
            return (
                0,
                json.dumps({
                    "slots": [{"tab": "AM", "slot": 1, "filled": False, "title": ""}]
                }).encode(),
                b"",
            )
        if argv[2] == "text":
            return 0, json.dumps({"text": "=== AM ===\n1.[]\n"}).encode(), b""
        raise AssertionError(argv)

    monkeypatch.setattr(rc.radio_news, "_run", fake_run)
    plan = asyncio.run(
        rc.plan_placement("DOC1", "weekday", "global", ["https://example.com/retry"])
    )
    assert plan["plan"][0]["action"] == "write"
    assert (plan["plan"][0]["tab"], plan["plan"][0]["slot"]) == ("AM", 1)


def test_apply_converted_rejects_unconverted_and_writes_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")

    from app.media import radio_loop as rl

    monkeypatch.setattr(rl, "_LEDGER", tmp_path / "ledger.jsonl")

    # 1. Unconverted piece -> 400 hard gate
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            rc.apply_converted("DOC1", "weekday", "global", ["https://example.com/missing"])
        )
    assert exc.value.status_code == 400

    # 2. Converted piece -> verbatim explicit write (zero LLM calls)
    rc.cache_put({
        "url": "https://example.com/ready",
        "title": "Ready Headline",
        "body": "Paragraph 1.\n\nParagraph 2.",
        "words": 4,
    })

    explicit_payloads: list[dict] = []

    async def fake_run(argv, timeout=90, stdin=None):
        cmd = argv[2]
        if cmd == "slots":
            return (
                0,
                json.dumps({
                    "slots": [{"tab": "AM", "slot": 1, "filled": bool(explicit_payloads), "title": ""}]
                }).encode(),
                b"",
            )
        if cmd == "text":
            return 0, json.dumps({"text": "=== AM ===\n1.[]\n"}).encode(), b""
        if cmd == "fill":
            assert "--explicit" in argv
            data = json.loads(stdin.decode())
            explicit_payloads.append(data)
            p = data["pieces"][0]
            return (
                0,
                json.dumps({
                    "written": [{"tab": p["tab"], "slot": p["slot"], "title": p["title"]}],
                    "skipped": [],
                }).encode(),
                b"",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(rc.radio_news, "_run", fake_run)

    res = asyncio.run(
        rc.apply_converted("DOC1", "weekday", "global", ["https://example.com/ready"])
    )
    assert len(res["written"]) == 1
    assert explicit_payloads[0]["pieces"][0] == {
        "tab": "AM",
        "slot": 1,
        "title": "Ready Headline",
        "body": "Paragraph 1.\n\nParagraph 2.",
    }
    assert res["doc_check"]["filled"] == 1


def test_convert_routes_preview_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "CACHE_DIR", tmp_path / "cache")

    async def fake_run(argv, timeout=90, stdin=None):
        if argv[2] == "slots":
            return (
                0,
                json.dumps({
                    "slots": [{"tab": "AM", "slot": 1, "filled": False, "title": ""}]
                }).encode(),
                b"",
            )
        if argv[2] == "text":
            return 0, json.dumps({"text": "=== AM ===\n1.[]\n"}).encode(), b""
        raise AssertionError(argv)

    monkeypatch.setattr(rc.radio_news, "_run", fake_run)

    app = FastAPI()
    app.include_router(rc.router)
    client = TestClient(app)

    r = client.post(
        "/api/newsroom/radio/news/preview",
        json={
            "doc_id": "DOC1",
            "kind": "weekday",
            "category": "global",
            "urls": ["https://example.com/u1"],
            "titles": {"https://example.com/u1": "Fallback Title"},
        },
    )
    assert r.status_code == 200
    assert r.json()["slots_empty"] == 1
    assert r.json()["plan"][0]["tab"] == "AM"
    assert client.get("/api/newsroom/radio/news/convert/nonexistent").status_code == 404
