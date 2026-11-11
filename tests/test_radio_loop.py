"""Tests for ``app.media.radio_loop`` — unified per-piece streaming pipeline
(Naz 2026-11-10):
- per-doc write ledger round-trip
- ``_filter_written`` never pre-skips on ledger alone (dedup is doc-verified)
- ``rewritten: true`` (IDE SCOUT) skips hub convert/cache (0 LLM calls) and
  writes verbatim via ``fill --explicit``
- raw pieces run ``convert_piece`` (or hit cache) and write via ``fill --explicit``
- convert timeout/failure writes ``[CONVERT FAILED — <url>]`` fail-safe into
  the slot without ledgering
- ``_write_explicit`` catches 504 timeout on attempt 1 and retries attempt 2
- post-loop ``doc_check`` + server-side ``format`` (``doc_format.py``) surfaced
  in ``loop_status``
"""

import asyncio
import json

from fastapi import HTTPException

from app.media import radio_loop as rl


def test_ledger_roundtrip_and_doc_verified_policy(tmp_path, monkeypatch):
    """Ledger records written slots; ``_filter_written`` never pre-skips on the
    ledger alone because dedup is doc-verified against live Doc text."""
    ledger = tmp_path / "radio_written.jsonl"
    monkeypatch.setattr(rl, "_LEDGER", ledger)

    rl._ledger_append("DOC1", "https://example.com/a", "Japan Rate Hike", {"tab": "AM", "slot": 1})
    urls, titles = rl._ledger_load("DOC1")
    assert "https://example.com/a" in urls
    assert "japan rate hike" in titles

    pieces = [
        {"url": "https://example.com/a", "title": "Different Headline"},
        {"url": "https://example.com/b", "title": "  japan   rate hike "},
        {"url": "https://example.com/c", "title": "Fresh Story"},
    ]
    verdicts = asyncio.run(rl._filter_written("DOC1", pieces))
    assert verdicts == [("pending", None), ("pending", None), ("pending", None)]


def test_run_loop_rewritten_piece_skips_convert_and_writes_explicit(tmp_path, monkeypatch):
    """An IDE-scouted piece (``rewritten: true``) must NOT call ``convert_piece``
    (zero hub LLM calls) — it splits title/body, runs advisory checks, and
    writes verbatim via ``fill --explicit``."""
    monkeypatch.setattr(rl, "_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(rl.radio_convert, "CACHE_DIR", tmp_path / "conv_cache")

    async def boom_convert(*a, **k):
        raise AssertionError("convert_piece must NOT run when rewritten=True")

    monkeypatch.setattr(rl.radio_convert, "convert_piece", boom_convert)
    monkeypatch.setattr(rl, "verify_figures", lambda s, r: {"ok": True, "checked": 1, "mismatches": []})
    monkeypatch.setattr(rl, "freshness_scan", lambda _t: [])

    explicit_writes: list[dict] = []
    format_calls: list[list[str]] = []

    async def fake_run(argv, timeout=90, stdin=None):
        if argv[1].endswith("doc_format.py"):
            format_calls.append(list(argv))
            return 0, json.dumps({"bolded": ["Person One"], "underlined": ["May 5"]}).encode(), b""
        cmd = argv[2]
        if cmd == "slots":
            return (
                0,
                json.dumps({
                    "slots": [
                        {"tab": "AM", "slot": 1, "filled": bool(explicit_writes), "title": "ASEAN Digital Pact"}
                    ]
                }).encode(),
                b"",
            )
        if cmd == "text":
            return 0, json.dumps({"text": "=== AM ===\n1.[]\n"}).encode(), b""
        if cmd == "fill":
            assert "--explicit" in argv
            d = json.loads(stdin.decode())
            explicit_writes.append(d["pieces"][0])
            p = d["pieces"][0]
            return (
                0,
                json.dumps({
                    "written": [{"tab": p["tab"], "slot": p["slot"], "title": p["title"]}],
                    "skipped": [],
                }).encode(),
                b"",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(rl.radio_news, "_run", fake_run)

    body_text = " ".join(["word"] * 195)
    pieces = [{
        "title": "ASEAN Digital Pact",
        "url": "https://example.com/asean",
        "region": "SEA",
        "content": body_text,
        "rewritten": True,
    }]
    job = {
        "loop_id": "t_ide",
        "status": "running",
        "started_at": 0.0,
        "finished_at": None,
        "total": 1,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "doc_check": None,
        "format": None,
        "pieces": [{
            "index": 0,
            "title": "ASEAN Digital Pact",
            "region": "SEA",
            "state": "pending",
            "passes": [],
            "verify": None,
            "slot": None,
            "words": 0,
            "error": None,
        }],
    }
    rl._JOBS["t_ide"] = job
    asyncio.run(rl._run_loop(job, "DOC_IDE", "weekday", "global", pieces, "dsh"))

    assert job["status"] == "done"
    assert job["done"] == 1
    assert job["pieces"][0]["state"] == "done"
    assert job["pieces"][0]["passes"][0]["pass"] == "ide_rewritten"
    assert len(explicit_writes) == 1
    assert explicit_writes[0]["title"] == "ASEAN Digital Pact"
    assert explicit_writes[0]["body"] == body_text
    assert len(format_calls) == 1
    st = rl.loop_status("t_ide")
    assert st is not None
    assert st["doc_check"]["filled"] == 1
    assert st["format"] == {"bolded": ["Person One"], "underlined": ["May 5"]}


def test_run_loop_converts_raw_piece_and_writes_explicit(tmp_path, monkeypatch):
    """A raw piece (`rewritten` unset/False) runs ``convert_piece`` on the fly
    and writes the converted ``(title, body)`` into the planned ``(tab, slot)``."""
    monkeypatch.setattr(rl, "_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(rl.radio_convert, "CACHE_DIR", tmp_path / "conv_cache")

    async def fake_convert(piece, model="dsh"):
        entry = {
            "url": piece["url"],
            "title": "Converted Radio Headline",
            "body": "Converted body paragraph one.\n\nConverted body paragraph two.",
            "words": 8,
            "title_problems": [],
            "verify_block": False,
            "figure_check": {"ok": True, "checked": 1, "mismatches": []},
            "freshness_hits": [],
            "passes": [{"pass": "pass1_rough", "ok": True}],
        }
        rl.radio_convert.cache_put(entry)
        return entry

    monkeypatch.setattr(rl.radio_convert, "convert_piece", fake_convert)

    explicit_writes: list[dict] = []

    async def fake_run(argv, timeout=90, stdin=None):
        if argv[1].endswith("doc_format.py"):
            return 0, json.dumps({"bolded": [], "underlined": []}).encode(), b""
        cmd = argv[2]
        if cmd == "slots":
            return (
                0,
                json.dumps({
                    "slots": [
                        {"tab": "AM", "slot": 2, "filled": bool(explicit_writes), "title": ""}
                    ]
                }).encode(),
                b"",
            )
        if cmd == "text":
            return 0, json.dumps({"text": "=== AM ===\n2.[]\n"}).encode(), b""
        if cmd == "fill":
            assert "--explicit" in argv
            d = json.loads(stdin.decode())
            explicit_writes.append(d["pieces"][0])
            p = d["pieces"][0]
            return (
                0,
                json.dumps({
                    "written": [{"tab": p["tab"], "slot": p["slot"], "title": p["title"]}],
                    "skipped": [],
                }).encode(),
                b"",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(rl.radio_news, "_run", fake_run)

    pieces = [{
        "title": "Raw Scout Headline",
        "url": "https://example.com/raw1",
        "region": "GLOBAL",
        "content": "Raw content " * 40,
    }]
    job = {
        "loop_id": "t_raw",
        "status": "running",
        "started_at": 0.0,
        "finished_at": None,
        "total": 1,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "doc_check": None,
        "format": None,
        "pieces": [{
            "index": 0,
            "title": "Raw Scout Headline",
            "region": "GLOBAL",
            "state": "pending",
            "passes": [],
            "verify": None,
            "slot": None,
            "words": 0,
            "error": None,
        }],
    }
    asyncio.run(rl._run_loop(job, "DOC_RAW", "weekday", "global", pieces, "dsh"))

    assert job["done"] == 1
    assert explicit_writes[0] == {
        "tab": "AM",
        "slot": 2,
        "title": "Converted Radio Headline",
        "body": "Converted body paragraph one.\n\nConverted body paragraph two.",
    }


def test_run_loop_writes_failsafe_placeholder_when_convert_fails(tmp_path, monkeypatch):
    """When ``convert_piece`` fails/times out on both attempts, the loop writes
    ``[CONVERT FAILED — <url>]`` INTO the assigned slot (state='failsafe') and
    does NOT ledger the URL so a re-run can replace it."""
    monkeypatch.setattr(rl, "_LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(rl.radio_convert, "CACHE_DIR", tmp_path / "conv_cache")

    attempts: list[int] = []

    async def failing_convert(piece, model="dsh"):
        attempts.append(1)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(rl.radio_convert, "convert_piece", failing_convert)

    explicit_writes: list[dict] = []

    async def fake_run(argv, timeout=90, stdin=None):
        if argv[1].endswith("doc_format.py"):
            return 0, b"{}", b""
        cmd = argv[2]
        if cmd == "slots":
            return (
                0,
                json.dumps({
                    "slots": [{"tab": "AM", "slot": 1, "filled": False, "title": ""}]
                }).encode(),
                b"",
            )
        if cmd == "text":
            return 0, json.dumps({"text": "=== AM ===\n1.[]\n"}).encode(), b""
        if cmd == "fill":
            d = json.loads(stdin.decode())
            explicit_writes.append(d["pieces"][0])
            p = d["pieces"][0]
            return (
                0,
                json.dumps({
                    "written": [{"tab": p["tab"], "slot": p["slot"], "title": p["title"]}],
                    "skipped": [],
                }).encode(),
                b"",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(rl.radio_news, "_run", fake_run)

    pieces = [{
        "title": "Stuck Story",
        "url": "https://example.com/stuck",
        "region": "GLOBAL",
        "content": "x" * 200,
    }]
    job = {
        "loop_id": "t_fs",
        "status": "running",
        "started_at": 0.0,
        "finished_at": None,
        "total": 1,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "doc_check": None,
        "format": None,
        "pieces": [{
            "index": 0,
            "title": "Stuck Story",
            "region": "GLOBAL",
            "state": "pending",
            "passes": [],
            "verify": None,
            "slot": None,
            "words": 0,
            "error": None,
        }],
    }
    asyncio.run(rl._run_loop(job, "DOC_FS", "weekday", "global", pieces, "dsh"))

    assert len(attempts) == 2  # initial try + 1 retry
    assert job["failed"] == 1
    assert job["pieces"][0]["state"] == "failsafe"
    assert "[CONVERT FAILED — https://example.com/stuck]" in explicit_writes[0]["body"]
    urls, _ = rl._ledger_load("DOC_FS")
    assert "https://example.com/stuck" not in urls


def test_write_explicit_retries_on_504_timeout(monkeypatch):
    """When ``radio_news._run`` raises HTTPException(504) on attempt 1,
    ``_write_explicit`` catches it and succeeds on attempt 2."""
    calls: list[int] = []

    async def flaky_run(argv, timeout=90, stdin=None):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPException(504, "radio news script timed out")
        return (
            0,
            json.dumps({
                "written": [{"tab": "AM", "slot": 1, "title": "Recovered"}],
                "skipped": [],
            }).encode(),
            b"",
        )

    monkeypatch.setattr(rl.radio_news, "_run", flaky_run)
    slot_info, err = asyncio.run(
        rl._write_explicit("DOC1", "weekday", "global", "AM", 1, "Recovered", "Body")
    )
    assert len(calls) == 2
    assert err is None
    assert slot_info == {"tab": "AM", "slot": 1, "title": "Recovered"}
