"""Newsroom panel backend — argv construction + error surfacing.

The skill scripts are the contract, so ``_run`` is monkeypatched (no real
newstank / Google Docs): each test captures the argv the route would exec and
feeds back a canned (rc, stdout, stderr).
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import newsroom, radio_news


def _client(monkeypatch, rc=0, out=b"{}", err=b""):
    """App with only the newsroom router; ``_run`` captures argv."""
    calls: list[list[str]] = []

    async def fake_run(argv, timeout=90):
        calls.append(list(argv))
        return rc, out, err

    monkeypatch.setattr(newsroom, "_run", fake_run)
    app = FastAPI()
    app.include_router(newsroom.router)
    return TestClient(app), calls


def test_strip_fabricated_thai():
    """Code guard: fabricated Thai (not in source) stripped across all observed
    formats; source-faithful Thai kept. Mirrors office Somatic 9befe5f."""
    from app.newsroom import _strip_fabricated_thai, _THAI_RUN_RE

    def has_thai(s: str) -> bool:
        return bool(_THAI_RUN_RE.search(s))

    src_en = "Bangkok Governor Chadchart Sittipunt spoke at the event."

    # 1. **[English(fabricated Thai)]** wrapper
    out = _strip_fabricated_thai(
        "Governor **[Chadchart Sittipunt(ชัชชัย วิษณุพงศ์)]** spoke.", src_en)
    assert "Chadchart Sittipunt" in out and not has_thai(out), out

    # 2. [English](fabricated Thai) markdown-link wrapper
    out = _strip_fabricated_thai("**[Sorasak](ศรัณย์ พงษ์เจริญวรกุล)** led.", src_en)
    assert "Sorasak" in out and not has_thai(out), out

    # 3. inline fabricated Thai, no wrapper
    out = _strip_fabricated_thai("The ministry กระทรวงศึกษาธิการ announced.", src_en)
    assert not has_thai(out), out

    # 4. source-faithful Thai is KEPT (source contains it)
    src_th = "รัฐมนตรี อนุทิน ชาญวีรกุล แถลงข่าว"
    out = _strip_fabricated_thai(
        "Minister **[Anutin Charnvirakul(อนุทิน ชาญวีรกุล)]** spoke.", src_th)
    assert "อนุทิน" in out and "ชาญวีรกุล" in out, out


def test_rewrite_convert_missing_and_valid(tmp_path, monkeypatch):
    """IDE rewrite CONVERT relay: soft-fail on missing/unparseable handoff, verbatim relay
    (Thai UTF-8 + **name**/~~date~~ markers preserved) on a valid one. Mirrors office
    test_rewrite_convert, but steers the module-level _REWRITE_HANDOFF constant (no Path patch)."""
    import asyncio
    from app import newsroom
    from app.newsroom import rewrite_convert

    miss = {"rewritten": "", "seo": "", "errors": ["no IDE handoff file — run 📋 IDE REWRITE first"]}

    monkeypatch.setattr(newsroom, "_REWRITE_HANDOFF", tmp_path / "absent.json")
    assert asyncio.run(rewrite_convert()) == miss                      # missing → soft-fail

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(newsroom, "_REWRITE_HANDOFF", bad)
    assert asyncio.run(rewrite_convert()) == miss                      # unparseable → soft-fail

    handoff = tmp_path / "latest.json"
    handoff.write_text(json.dumps({
        "rewritten": "EN: Title\nTH: หัวข้อ\n\nBody with **name** and ~~date~~ markers.",
        "seo": "## AI SEO BLOCK\nSummary.",
    }), encoding="utf-8")
    monkeypatch.setattr(newsroom, "_REWRITE_HANDOFF", handoff)
    assert asyncio.run(rewrite_convert()) == {                         # valid → verbatim relay
        "rewritten": "EN: Title\nTH: หัวข้อ\n\nBody with **name** and ~~date~~ markers.",
        "seo": "## AI SEO BLOCK\nSummary.",
        "errors": [],
    }


# ---------------------------------------------------------------- queue


def test_queue_defaults_to_chompatsorn(monkeypatch):
    payload = {"date": "2026-07-22", "author": "Chompatsorn", "count": 0, "articles": []}
    c, calls = _client(monkeypatch, out=json.dumps(payload).encode())
    r = c.get("/api/newsroom/queue")
    assert r.status_code == 200
    assert r.json()["count"] == 0
    argv = calls[0]
    assert argv[0] == "python3"  # exec via interpreter — vault scripts carry no exec bit
    assert argv[1].endswith("queue.py")
    assert argv[2:4] == ["list", "--json"]
    assert argv[4:7] == ["--all", "--author", "Chompatsorn"]  # --all keeps sent rows (dimmed in UI)
    assert "--date" not in argv


def test_queue_passes_date_and_author(monkeypatch):
    c, calls = _client(monkeypatch, out=b"{}")
    assert c.get("/api/newsroom/queue?date=2026-07-21&author=all").status_code == 200
    argv = calls[0]
    assert argv[2:] == ["list", "--json", "--all", "--author", "all", "--date", "2026-07-21"]


def test_story_show(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"id": "123"}')
    assert c.get("/api/newsroom/story/123").json()["id"] == "123"
    assert calls[0][2:] == ["show", "123", "--json"]


def test_mark_requires_ids(monkeypatch):
    c, calls = _client(monkeypatch)
    assert c.post("/api/newsroom/mark", json={"ids": []}).status_code == 400
    assert calls == []  # nothing exec'd


def test_mark_with_doc(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"marked": 2}')
    r = c.post("/api/newsroom/mark", json={"ids": ["a1", "b2"], "doc_id": "D"})
    assert r.status_code == 200
    assert calls[0][2:] == ["mark", "a1", "b2", "--doc", "D"]


# ---------------------------------------------------------------- append


def test_append_requires_text(monkeypatch):
    c, calls = _client(monkeypatch)
    assert c.post("/api/newsroom/append", json={"text": "  "}).status_code == 400
    assert calls == []


def test_append_today_vs_doc(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"appended": true}')
    assert c.post("/api/newsroom/append", json={"text": "script"}).status_code == 200
    assert calls[0][2:] == ["--today", "--text", "script"]
    assert c.post("/api/newsroom/append", json={"text": "s", "doc_id": "D"}).status_code == 200
    assert calls[1][2:] == ["--doc", "D", "--text", "s"]


def test_append_tab_passthrough(monkeypatch):
    """tab AM/MID/EVE → --tab in argv; NL (default, any case) → no --tab."""
    c, calls = _client(monkeypatch, out=b'{"appended": true}')
    assert c.post("/api/newsroom/append", json={"text": "s", "tab": "MID"}).status_code == 200
    assert calls[0][2:] == ["--tab", "MID", "--today", "--text", "s"]
    assert c.post("/api/newsroom/append", json={"text": "s", "tab": "nl"}).status_code == 200
    assert calls[1][2:] == ["--today", "--text", "s"]  # NL normalised → default, no --tab


# ---------------------------------------------------------------- errors


def test_script_failure_surfaces_stderr_tail(monkeypatch):
    c, _ = _client(monkeypatch, rc=1, err=b"boom: newstank login failed")
    r = c.get("/api/newsroom/ledger")
    assert r.status_code == 502
    assert "newstank login failed" in r.json()["detail"]


def test_fatal_payload_becomes_400(monkeypatch):
    c, _ = _client(monkeypatch, out=b'{"_fatal": "no creds"}')
    r = c.get("/api/newsroom/queue")
    assert r.status_code == 400
    assert r.json()["detail"] == "no creds"


def test_probe_ok_and_down(monkeypatch):
    c, _ = _client(monkeypatch, rc=0, out=b"{}")
    assert c.get("/api/newsroom/probe").json() == {"ok": True}
    c2, _ = _client(monkeypatch, rc=3)
    assert c2.get("/api/newsroom/probe").json() == {"ok": False}


# ---------------------------------------------------------------- radio
# `radio.py` lives in the newsroom skill dir (vault copy = the deployed one),
# so load it by path rather than importing a repo module. The two skill dirs
# are kept byte-identical — accept either.


def _load_radio():
    import importlib.util
    import sys

    for p in (newsroom.SCRIPTS / "radio.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "radio.py"):
        if p.exists():
            # radio.py's `from docfill import ...` only resolves if the skill
            # dir is on sys.path (true when run as `python3 radio.py`, not true
            # for a by-path importlib load) — add it so _DOCFILL loads for real.
            script_dir = str(p.parent)
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)
            spec = importlib.util.spec_from_file_location("radio_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("radio.py not on a skill path yet (canonical writes pending)")


def test_radio_build_plan_aug2026():
    radio = _load_radio()
    plan = radio.build_plan(2026, 8, "202608 August")
    # sheet first, named after the folder.
    assert plan[0] == {"template_id": radio.TEMPLATE_SHEET,
                       "name": "202608 August", "kind": "sheet"}
    by_name = {it["name"]: it["kind"] for it in plan}
    # 2026-08-01 is Saturday, 2026-08-03 is Monday.
    assert by_name["20260801_Weekend Script"] == "weekend"
    assert by_name["20260803_Weekday Script"] == "weekday"
    counts = {}
    for it in plan:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
    assert counts == {"sheet": 1, "weekend": 10, "weekday": 21}  # Aug 2026 = 31 days


def test_radio_dry_run_makes_no_network_calls(monkeypatch, capsys):
    radio = _load_radio()

    def boom(*a, **k):
        raise AssertionError("dry-run touched the network")

    # find/existing are stubbed (the only calls dry-run makes); google_token +
    # copy_file explode if hit — proving dry-run neither auths nor writes.
    monkeypatch.setattr(radio, "google_token", boom)
    monkeypatch.setattr(radio, "copy_file", boom)
    monkeypatch.setattr(radio, "find_month_folder",
                        lambda *a, **k: ("FOLDER_ID", "202608 August"))
    monkeypatch.setattr(radio, "existing_names", lambda *a, **k: set())

    radio.main(["--year", "2026", "--month", "8", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["folder"] == {"id": "FOLDER_ID", "name": "202608 August"}
    assert out["counts"] == {"sheet": 1, "weekend": 10, "weekday": 21,
                             "planned": 32, "to_create": 32, "skipped": 0}
    assert out["created"] == []
    assert out["to_create"][0] == {"name": "202608_Rundown", "kind": "sheet"}
    assert len(out["to_create"]) == 32


def test_radio_dry_run_skips_existing(monkeypatch, capsys):
    radio = _load_radio()
    monkeypatch.setattr(radio, "google_token", lambda *a, **k: pytest.fail("net"))
    monkeypatch.setattr(radio, "copy_file", lambda *a, **k: pytest.fail("write"))
    monkeypatch.setattr(radio, "find_month_folder",
                        lambda *a, **k: ("F", "202608 August"))
    # the sheet already exists → idempotent skip (sheet is now named YYYYMM_Rundown).
    monkeypatch.setattr(radio, "existing_names",
                        lambda *a, **k: {"202608_Rundown"})
    radio.main(["--year", "2026", "--month", "8", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["to_create"] == 31
    assert out["counts"]["skipped"] == 1


def test_radio_generate_stamps_calendar_per_doc(monkeypatch, capsys):
    """Real (non-dry-run) generate stamps CALENDAR on every daily doc (not the
    sheet), using the broadcast date convention 'Weekday, Month Nth, Year'."""
    radio = _load_radio()
    monkeypatch.setattr(radio, "find_month_folder",
                        lambda *a, **k: ("F", "202608 August"))
    monkeypatch.setattr(radio, "existing_names", lambda *a, **k: set())
    monkeypatch.setattr(radio, "copy_file",
                        lambda tid, name, fid: (f"id-{name}", name, "link"))
    stamped = []
    monkeypatch.setattr(radio, "replace_calendar",
                        lambda doc_id, date_text: stamped.append((doc_id, date_text)))
    radio.main(["--year", "2026", "--month", "8"])  # real run, no --dry-run
    json.loads(capsys.readouterr().out)
    # Aug 2026 = 31 days → 31 daily docs stamped; the 1 sheet is NOT stamped.
    assert len(stamped) == 31
    assert all("Script" in doc_id for doc_id, _ in stamped)
    assert not any("Rundown" in doc_id for doc_id, _ in stamped)
    # first daily doc = day 1; date text follows the broadcast convention.
    assert "20260801" in stamped[0][0]
    assert stamped[0][1] == radio.calendar_text("20260801")
    assert stamped[0][1].endswith("2026")


def test_radio_parse_title_and_body():
    radio = _load_radio()
    en, body = radio._parse_title_and_body(
        "EN: Peace Talks\nTH: สันติภาพ\n\nOn ~~July~~, **X** spoke.")
    assert en == "Peace Talks"
    assert body == "On ~~July~~, **X** spoke."  # TH title dropped for radio
    en2, body2 = radio._parse_title_and_body("plain body only")
    assert en2 is None and body2 == "plain body only"  # backward compat


def test_radio_fill_underlines_date_backstop(monkeypatch, capsys):
    """Regression: SEND TO RADIO used to rely 100% on Ben's ~~..~~ markers for
    underlining — a date/relative-time the model failed to wrap (LLM compliance
    miss) landed with ZERO underline. fill_radio_slot now runs the same DATE_RE
    backstop nl_append.py already had, so a missed date still underlines."""
    radio = _load_radio()

    monkeypatch.setattr(radio, "_api", lambda method, url, body=None, params=None: {
        "tabs": [{"tabProperties": {"tabId": "tab1", "title": "AM"}}]
    })

    def fake_find_heading(api_get, doc_id, tab_id, match, after=0):
        if match("NATIONAL NEWS", "HEADING_1"):
            return {"startIndex": 10, "endIndex": 20,
                    "text": "NATIONAL NEWS", "next_start": 20}
        return {"startIndex": 21, "endIndex": 40,
                "text": "1.[Old headline]", "next_start": 100}

    monkeypatch.setattr(radio, "find_heading", fake_find_heading)

    # No ~~..~~ marker around "today" — simulates the model missing the markup.
    text = "EN: New title\nTH: หัวข้อ\n\nThe story happened today, officials said."
    radio.fill_radio_slot(None, None, None, section="AM", block="NATIONAL",
                          slot_n=1, text=text, doc="DOC123", dry=True)

    out = json.loads(capsys.readouterr().out)
    assert out["underline_spans"] >= 1  # "today" caught by the DATE_RE backstop


def test_radio_preview_argv(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"dry_run": true}')
    assert c.post("/api/newsroom/radio/preview",
                  json={"year": 2026, "month": 8}).status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("radio.py")
    assert argv[2:8] == ["--year", "2026", "--month", "8", "--dry-run"]
    assert "--sheet-name" not in argv


def test_radio_preview_passes_sheet_name(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"dry_run": true}')
    c.post("/api/newsroom/radio/preview",
           json={"year": 2026, "month": 8, "sheet_name": "Aug Rundown"})
    argv = calls[0]
    # preview appends --dry-run after the sheet-name pair, so assert the pair by
    # position rather than expecting it to be the final two args.
    i = argv.index("--sheet-name")
    assert argv[i + 1] == "Aug Rundown"
    assert "--dry-run" in argv


def test_radio_generate_omits_dry_run(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"created": []}')
    assert c.post("/api/newsroom/radio/generate",
                  json={"year": 2026, "month": 8}).status_code == 200
    argv = calls[0]
    assert "--dry-run" not in argv
    assert argv[1].endswith("radio.py")


def test_radio_requires_year_and_month(monkeypatch):
    c, calls = _client(monkeypatch)
    assert c.post("/api/newsroom/radio/preview", json={"year": 2026}).status_code == 400
    assert c.post("/api/newsroom/radio/preview", json={"month": 8}).status_code == 400
    assert c.post("/api/newsroom/radio/preview", json={}).status_code == 400
    assert c.post("/api/newsroom/radio/generate", json={}).status_code == 400
    assert calls == []  # nothing exec'd on a bad body


# ---------------------------------------------------------------- newsline
# `newsline.py` lives in the newsroom skill dir (vault copy = the deployed one),
# loaded by path exactly like radio.py above. Pure helpers are tested directly;
# the Drive/Docs calls stay offline via monkeypatch.


def _load_newsline():
    import importlib.util

    for p in (newsroom.SCRIPTS / "newsline.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "newsline.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location("newsline_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("newsline.py not on a skill path yet (canonical writes pending)")


def test_nl_doc_name_for_format():
    nl = _load_newsline()
    assert nl.doc_name_for(2026, 7, 1) == "NL & NWB 010726"
    assert nl.doc_name_for(2026, 7, 31) == "NL & NWB 310726"
    # YY wraps mod 100 (year 2100 → 00).
    assert nl.doc_name_for(2100, 12, 9) == "NL & NWB 091200"


def test_nl_ddmmyy_str():
    nl = _load_newsline()
    from datetime import date
    assert nl._ddmmyy_str(date(2026, 7, 1)) == "01.JULY.2026"
    assert nl._ddmmyy_str(date(2026, 7, 31)) == "31.JULY.2026"


def test_nl_date_long_str_weekday_and_ordinal():
    nl = _load_newsline()
    from datetime import date
    # 2026-07-31 = Friday; ordinal 31st.
    assert nl._date_long_str(date(2026, 7, 31)) == "Friday, July 31st of 2026"
    # 2026-07-01 = Wednesday; day 1 → 1st (no leading zero).
    assert nl._date_long_str(date(2026, 7, 1)) == "Wednesday, July 1st of 2026"
    # ordinal edge cases (11th–13th → th; 21st → st).
    assert nl._date_long_str(date(2026, 7, 11)).endswith("11th of 2026")
    assert nl._date_long_str(date(2026, 7, 21)).endswith("21st of 2026")
    assert nl._date_long_str(date(2026, 7, 13)).endswith("13th of 2026")


def test_nl_build_plan_day_counts():
    nl = _load_newsline()
    jul = nl.build_plan(2026, 7)
    assert len(jul) == 31
    assert jul[0]["name"] == "NL & NWB 010726"
    assert jul[-1]["name"] == "NL & NWB 310726"
    # every entry carries its datetime.date for stamping.
    from datetime import date
    assert jul[0]["date"] == date(2026, 7, 1)
    assert all("date" in it and "day" in it for it in jul)
    # Feb 2026 is not a leap year → 28 days.
    assert len(nl.build_plan(2026, 2)) == 28


def test_nl_preview_makes_no_writes(monkeypatch, capsys):
    """Preview is read-only: no folder creation, no copy, no stamping — it only
    reads folder existence + names. google_token + create_folder + copy_file +
    stamp_dates must never fire."""
    nl = _load_newsline()

    def boom(*a, **k):
        raise AssertionError("preview touched a write path")

    monkeypatch.setattr(nl, "google_token", boom)
    monkeypatch.setattr(nl, "create_folder", boom)
    monkeypatch.setattr(nl, "copy_file", boom)
    monkeypatch.setattr(nl, "stamp_dates", boom)
    # Both folders reported as missing → dry path returns would-create flags
    # without touching the network beyond the read (which we stub to "absent").
    monkeypatch.setattr(nl, "_find_folder_by_name", lambda *a, **k: (None, None))
    monkeypatch.setattr(nl, "_find_folder_by_prefix", lambda *a, **k: (None, None))

    nl.main(["--year", "2026", "--month", "7", "preview"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["year_folder"] == {"id": None, "name": "2026", "created": True}
    assert out["month_folder"] == {"id": None, "name": "202607 JUL", "created": True}
    assert out["counts"] == {"planned": 31, "to_create": 31, "skipped": 0}
    assert out["created"] == []
    assert out["to_create"][0] == {"name": "NL & NWB 010726", "day": 1}
    assert len(out["to_create"]) == 31


def test_nl_preview_skips_existing_docs(monkeypatch, capsys):
    nl = _load_newsline()
    monkeypatch.setattr(nl, "google_token", lambda *a, **k: pytest.fail("net"))
    monkeypatch.setattr(nl, "copy_file", lambda *a, **k: pytest.fail("write"))
    # year + month folders exist; a couple of daily docs already present.
    monkeypatch.setattr(nl, "_find_folder_by_name",
                        lambda parent, name: ("YID", name) if name == "2026" else (None, None))
    monkeypatch.setattr(nl, "_find_folder_by_prefix",
                        lambda parent, prefix: ("MID", "202607 JUL"))
    monkeypatch.setattr(nl, "existing_names",
                        lambda fid: {"NL & NWB 010726", "NL & NWB 020726"} if fid == "MID" else set())
    nl.main(["--year", "2026", "--month", "7", "preview"])
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["planned"] == 31
    assert out["counts"]["to_create"] == 29
    assert out["counts"]["skipped"] == 2
    assert out["year_folder"]["created"] is False
    assert out["month_folder"]["created"] is False


def test_nl_generate_creates_folders_and_stamps(monkeypatch, capsys):
    """Real generate: missing folders are created, each missing daily doc is
    copied + stamped exactly once; existing docs are skipped (never re-stamped)."""
    nl = _load_newsline()
    created_folders = []
    monkeypatch.setattr(nl, "_find_folder_by_name", lambda *a, **k: (None, None))
    monkeypatch.setattr(nl, "_find_folder_by_prefix", lambda *a, **k: (None, None))
    monkeypatch.setattr(nl, "create_folder",
                        lambda name, parent: created_folders.append((name, parent)) or f"id-{name}")
    monkeypatch.setattr(nl, "existing_names", lambda fid: set())
    copied = []
    stamped = []
    monkeypatch.setattr(nl, "copy_file",
                        lambda tid, name, fid: (copied.append(name) or (f"doc-{name}", name, f"link-{name}")))
    monkeypatch.setattr(nl, "stamp_dates",
                        lambda doc_id, d: stamped.append((doc_id, d)))
    nl.main(["--year", "2026", "--month", "7", "generate"])
    out = json.loads(capsys.readouterr().out)
    # year folder created, then month folder created inside it.
    assert created_folders == [("2026", nl.NL_HOME), ("202607 JUL", "id-2026")]
    assert out["dry_run"] is False
    assert out["year_folder"]["created"] is True
    assert out["month_folder"]["created"] is True
    # 31 days → 31 copies + 31 stamps, each stamped with its own date.
    assert len(copied) == 31
    assert len(stamped) == 31
    from datetime import date
    assert stamped[0] == ("doc-NL & NWB 010726", date(2026, 7, 1))
    assert stamped[-1][1] == date(2026, 7, 31)
    assert out["created"][0]["link"] == "link-NL & NWB 010726"
    assert len(out["created"]) == 31


def test_nl_generate_skips_existing_never_restamps(monkeypatch, capsys):
    """Idempotency: a full month re-run copies + stamps NOTHING (everything skipped)."""
    nl = _load_newsline()
    monkeypatch.setattr(nl, "_find_folder_by_name",
                        lambda parent, name: ("YID", name) if name == "2026" else (None, None))
    monkeypatch.setattr(nl, "_find_folder_by_prefix",
                        lambda parent, prefix: ("MID", "202607 JUL"))
    # every daily doc already exists.
    all_names = {nl.doc_name_for(2026, 7, d) for d in range(1, 32)}
    monkeypatch.setattr(nl, "existing_names", lambda fid: all_names)

    def boom(*a, **k):
        raise AssertionError("generate re-touched an existing doc")
    monkeypatch.setattr(nl, "copy_file", boom)
    monkeypatch.setattr(nl, "stamp_dates", boom)
    monkeypatch.setattr(nl, "create_folder", boom)
    nl.main(["--year", "2026", "--month", "7", "generate"])
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["to_create"] == 0
    assert out["counts"]["skipped"] == 31
    assert out["created"] == []
    assert len(out["skipped"]) == 31


def test_nl_requires_valid_month(monkeypatch, capsys):
    nl = _load_newsline()
    # _fatal prints the contract payload then sys.exit(1) — catch the exit and
    # read the captured stdout (the route layer turns _fatal into a clean 400).
    with pytest.raises(SystemExit):
        nl.main(["--year", "2026", "--month", "13", "preview"])
    out = json.loads(capsys.readouterr().out)
    assert out["_fatal"] == "month must be 1-12"


def test_nl_preview_argv(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"dry_run": true}')
    assert c.post("/api/newsroom/newsline/preview",
                  json={"year": 2026, "month": 7}).status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("newsline.py")
    assert argv[2:6] == ["--year", "2026", "--month", "7"]
    assert argv[-1] == "preview"


def test_nl_generate_argv(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"created": []}')
    assert c.post("/api/newsroom/newsline/generate",
                  json={"year": 2026, "month": 7}).status_code == 200
    argv = calls[0]
    assert argv[1].endswith("newsline.py")
    assert argv[2:6] == ["--year", "2026", "--month", "7"]
    assert argv[-1] == "generate"


def test_nl_requires_year_and_month(monkeypatch):
    c, calls = _client(monkeypatch)
    assert c.post("/api/newsroom/newsline/preview", json={"year": 2026}).status_code == 400
    assert c.post("/api/newsroom/newsline/preview", json={"month": 7}).status_code == 400
    assert c.post("/api/newsroom/newsline/preview", json={}).status_code == 400
    assert c.post("/api/newsroom/newsline/generate", json={}).status_code == 400
    assert calls == []  # nothing exec'd on a bad body


# ---------------------------------------------------------------- radio/news
# RADIO ▸ News Fill: `radio_news.py` fronts the radio-news skill script.
# `_rn_client` mirrors `_client` but mounts radio_news.router and captures the
# stdin payload the apply route feeds the child (the write path is stdin-driven).


def _rn_client(monkeypatch, rc=0, out=b"{}", err=b""):
    """App with only the radio_news router; ``_run`` captures argv + stdin."""
    calls: list[list[str]] = []
    stdins: list[bytes | None] = []

    async def fake_run(argv, timeout=90, stdin=None):
        calls.append(list(argv))
        stdins.append(stdin)
        return rc, out, err

    monkeypatch.setattr(radio_news, "_run", fake_run)
    app = FastAPI()
    app.include_router(radio_news.router)
    return TestClient(app), calls, stdins


def _load_radio_news():
    import importlib.util

    for p in (newsroom.SCRIPTS / "radio_news.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "radio_news.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location("radio_news_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("radio_news.py not on a skill path yet (canonical writes pending)")


# --- pure-function tests (skip if the deployed script isn't reachable) ---


def test_rn_build_slotmap_sizes():
    rn = _load_radio_news()
    assert len(rn.build_slotmap("global", "weekday")) == 10
    assert len(rn.build_slotmap("global", "weekend")) == 7
    assert len(rn.build_slotmap("business", "weekday")) == 10
    assert len(rn.build_slotmap("business", "weekend")) == 6


def test_rn_locate_slots_label_anchored():
    rn = _load_radio_news()

    def para(text, style, start):
        return {"startIndex": start, "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": [{"startIndex": start, "textRun": {"content": text}}]}}

    paragraphs = [
        para("Global/AM/CALENDAR\n", "NORMAL_TEXT", 1),
        para("3.[]\n", "HEADING_2", 20),
        para("National/AM/CALENDAR\n", "NORMAL_TEXT", 40),
        para("1.[]\n", "HEADING_2", 60),
    ]
    slots = rn.locate_slots(paragraphs)
    # Each slot heading binds to the label line above it, not across labels.
    assert ("Global", 3) in slots
    assert ("National", 1) in slots
    assert ("Global", 1) not in slots  # no cross-label collision


def test_rn_tab_bodies_documenttab_path():
    """Regression: a native-tab Docs response nests body under
    ``documentTab.body.content`` and every write needs the tab's ``tabId``.
    Reading ``body.content`` (the old bug) yields zero paragraphs → every slot
    'not found' → silent no-op fill."""
    rn = _load_radio_news()
    para = {"paragraph": {"elements": [{"textRun": {"content": "1.[]\n"}}]}}
    doc = {"tabs": [{
        "tabProperties": {"title": "AM", "tabId": "t.0"},
        # decoy: an empty top-level body must NOT be what we read
        "body": {"content": []},
        "documentTab": {"body": {"content": [
            {"sectionBreak": {}},           # non-paragraph block is filtered out
            para,
        ]}},
    }]}
    bodies = rn.tab_bodies(doc)
    assert list(bodies.keys()) == ["AM"]
    assert bodies["AM"]["tab_id"] == "t.0"
    assert bodies["AM"]["paras"] == [para]  # pulled from documentTab, decoy ignored


def test_rn_request_builders_tab_scoped():
    """Regression: multi-tab writes must carry tabId, and deleteContentRange
    takes a ``range`` object (not bare start/end). Both were wrong in the first
    build and only surfaced on a live batchUpdate (400)."""
    rn = _load_radio_news()
    _, ins = rn._ins(42, "t.1", "hello")
    assert ins["insertText"]["location"] == {"index": 42, "tabId": "t.1"}
    assert ins["insertText"]["text"] == "hello"
    idx, dele = rn._del(10, 18, "t.1")
    assert idx == 10  # sort key is the start index
    assert dele["deleteContentRange"]["range"] == {
        "startIndex": 10, "endIndex": 18, "tabId": "t.1"}


def test_rn_slice_leadin_fill():
    rn = _load_radio_news()
    out = rn.slice_leadin_fill("X [ARTICLE HEADLINE] Y [SOURCE] Z", "Big News", "Reuters")
    assert "Big News" in out
    assert "Reuters" in out
    assert "[ARTICLE HEADLINE]" not in out
    assert "[SOURCE]" not in out


# --- route tests (always run; _run is monkeypatched) ---


def test_rn_report_happy_and_fatal(monkeypatch):
    payload = {"category": "global", "results": [], "count": 0,
               "slice_of_life": None, "mtime": "2026-07-27T00:00:00Z"}
    c, _, _ = _rn_client(monkeypatch, out=json.dumps(payload).encode())
    r = c.get("/api/newsroom/radio/news/report")
    assert r.status_code == 200
    assert r.json()["category"] == "global"

    c2, _, _ = _rn_client(monkeypatch, out=b'{"_fatal": "handoff missing"}')
    r2 = c2.get("/api/newsroom/radio/news/report")
    assert r2.status_code == 400
    assert r2.json()["detail"] == "handoff missing"


def test_rn_docs_argv_and_limit(monkeypatch):
    c, calls, _ = _rn_client(monkeypatch, out=b"[]")
    assert c.get("/api/newsroom/radio/news/docs").status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("radio_news.py")
    assert argv[-1] == "list-docs"

    c2, calls2, _ = _rn_client(monkeypatch, out=b"[]")
    assert c2.get("/api/newsroom/radio/news/docs?limit=5").status_code == 200
    argv2 = calls2[0]
    assert argv2[-3:] == ["list-docs", "--limit", "5"]


def test_rn_browse_argv_and_parent(monkeypatch):
    c, calls, _ = _rn_client(monkeypatch, out=b'{"folders":[],"docs":[]}')
    assert c.get("/api/newsroom/radio/news/browse").status_code == 200
    assert calls[0][-1] == "browse"  # default parent handled by the script

    c2, calls2, _ = _rn_client(monkeypatch, out=b'{"folders":[],"docs":[]}')
    assert c2.get("/api/newsroom/radio/news/browse?parent=FOLDERID").status_code == 200
    assert calls2[0][-3:] == ["browse", "--parent", "FOLDERID"]


def test_rn_split_children_partitions_and_sorts():
    rn = _load_radio_news()
    children = [
        {"id": "d1", "name": "20260901_Weekday Script", "mimeType": "application/vnd.google-apps.document"},
        {"id": "d2", "name": "20260903_Weekend Script", "mimeType": "application/vnd.google-apps.document"},
        {"id": "noise", "name": "random notes", "mimeType": "application/vnd.google-apps.document"},
        {"id": "fB", "name": "202608 August", "mimeType": "application/vnd.google-apps.folder"},
        {"id": "fA", "name": "202607 July", "mimeType": "application/vnd.google-apps.folder"},
    ]
    folders, docs = rn.split_children(children)
    # folders sorted by name (YYYYMM prefix → chronological)
    assert [f["name"] for f in folders] == ["202607 July", "202608 August"]
    # only NAME_RE docs kept, newest-first, kind parsed
    assert [d["id"] for d in docs] == ["d2", "d1"]
    assert docs[0]["kind"] == "weekend" and docs[1]["kind"] == "weekday"
    assert "noise" not in [d["id"] for d in docs]


def test_rn_apply_requires_fields(monkeypatch):
    c, calls, _ = _rn_client(monkeypatch)
    assert c.post("/api/newsroom/radio/news/apply",
                  json={"kind": "weekday", "category": "global"}).status_code == 400
    assert c.post("/api/newsroom/radio/news/apply",
                  json={"doc_id": "D", "category": "global"}).status_code == 400
    assert c.post("/api/newsroom/radio/news/apply",
                  json={"doc_id": "D", "kind": "weekday"}).status_code == 400
    assert calls == []  # nothing exec'd on a bad body


def test_rn_apply_argv_and_stdin(monkeypatch):
    c, calls, stdins = _rn_client(monkeypatch, out=b'{"written": [], "skipped": []}')
    body = {"doc_id": "DOC1", "kind": "weekday", "category": "global",
            "pieces": [{"title": "T", "url": "U", "source": "S",
                        "date": "2026-07-27", "content": "C", "words": 100}],
            "slice": {"title": "slice title"}}
    r = c.post("/api/newsroom/radio/news/apply", json=body)
    assert r.status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("radio_news.py")
    assert argv[2:] == ["fill", "--doc", "DOC1", "--kind", "weekday", "--category", "global"]
    sent = json.loads(stdins[0])
    assert sent["pieces"][0]["title"] == "T"
    assert sent["slice"]["title"] == "slice title"


# --- SEA-lead placement (assign_pieces) + AUTOPILOT autofill ---


def test_rn_assign_pieces_sea_leads_each_broadcast():
    rn = _load_radio_news()
    slotmap = rn.build_slotmap("global", "weekday")  # slot-1 at AM/MIDDAY/EVE
    pieces = ([{"title": "sea%d" % i, "region": "SEA"} for i in range(3)]
              + [{"title": "g%d" % i} for i in range(7)])
    a = rn.assign_pieces(slotmap, pieces, "global")
    for tab in ("AM", "MIDDAY", "EVE"):  # every broadcast lead is a SEA piece
        assert a[(tab, 1)]["region"] == "SEA"
    # non-lead slots are the non-SEA remainder, newest-first into earliest slot
    assert a[("AM", 2)]["title"] == "g0"
    assert all(a[k].get("region") != "SEA" for k in a if k[1] != 1)
    assert len(a) == len(slotmap) == 10


def test_rn_assign_pieces_business_sequential_ignores_region():
    rn = _load_radio_news()
    slotmap = rn.build_slotmap("business", "weekday")  # no slot 1 anywhere
    pieces = [{"title": "b%d" % i, "region": "SEA"} for i in range(10)]
    a = rn.assign_pieces(slotmap, pieces, "business")
    assert a[slotmap[0]]["title"] == "b0"  # sequential; region tag irrelevant
    assert a[slotmap[1]]["title"] == "b1"
    assert len(a) == 10


def test_rn_assign_pieces_short_sea_falls_back():
    rn = _load_radio_news()
    slotmap = rn.build_slotmap("global", "weekend")  # 2 leads (MIDDAY/EVE), 7 slots
    pieces = ([{"title": "sea0", "region": "SEA"}]
              + [{"title": "g%d" % i} for i in range(6)])  # only 1 SEA
    a = rn.assign_pieces(slotmap, pieces, "global")
    assert a[("MIDDAY", 1)]["title"] == "sea0"          # lone SEA leads MIDDAY
    assert a[("EVE", 1)].get("region") != "SEA"          # EVE lead falls back to non-SEA
    assert len(a) == len(slotmap) == 7


def test_rn_autofill_argv_no_stdin(monkeypatch):
    c, calls, stdins = _rn_client(
        monkeypatch, out=b'{"written": [], "skipped": [], "auto": true}')
    r = c.post("/api/newsroom/radio/news/autofill",
               json={"doc_id": "DOC9", "kind": "weekend", "category": "global"})
    assert r.status_code == 200
    assert calls[0][2:] == ["autofill", "--doc", "DOC9",
                            "--kind", "weekend", "--category", "global"]
    assert stdins[0] is None  # autofill reads the handoff itself — no stdin


def test_rn_autofill_requires_fields(monkeypatch):
    c, calls, _ = _rn_client(monkeypatch)
    assert c.post("/api/newsroom/radio/news/autofill",
                  json={"kind": "weekday", "category": "global"}).status_code == 400
    assert calls == []  # nothing exec'd on a bad body


# --- glm-5 rewrite pre-pass (Editor Ben's voice) ---
#
# The live rewrite hits the OmniRoute gateway; tests stay offline by either
# monkeypatching ``_gateway`` or exercising the pass-through short-circuits
# (RADIO_REWRITE=off / rewritten:true), which must make ZERO gateway contact.


def test_rn_parse_rewrite_strict_false_literal_newlines():
    """The gem body carries literal newlines between paragraphs — strict JSON
    rejects control chars in strings, so the parser must use strict=False."""
    rn = _load_radio_news()
    raw = '{"title": "Bangkok Repeats", "body": "Para one.\nPara two."}'
    title, body = rn._parse_rewrite(raw, "orig title")
    assert title == "Bangkok Repeats"
    assert body == "Para one.\nPara two."


def test_rn_parse_rewrite_strips_code_fence():
    rn = _load_radio_news()
    raw = '```json\n{"title": "T", "body": "B"}\n```'
    assert rn._parse_rewrite(raw, "orig") == ("T", "B")


def test_rn_parse_rewrite_garble_falls_back():
    """Non-JSON reply → (original title, returned text) — never a crash."""
    rn = _load_radio_news()
    assert rn._parse_rewrite("not json at all", "orig") == ("orig", "not json at all")


def test_rn_parse_rewrite_prose_prefix():
    """Regression (live bug, 2026-07-28): the model sometimes wraps the JSON in
    prose + a code fence. The old parser only stripped a fence if the text
    *started* with one, so json.loads failed and the raw JSON dumped into the
    body slot. raw_decode from the first '{' tolerates any prefix + trailing."""
    rn = _load_radio_news()
    raw = 'Here is the rewrite:\n```json\n{"title": "T", "body": "Good body."}\n```\nHope it helps.'
    assert rn._parse_rewrite(raw, "orig") == ("T", "Good body.")


def test_rn_parse_rewrite_think_prefix():
    """Regression (same bug): a reasoning model may emit a <think> block before
    the JSON. Must still parse the object behind it, not dump the raw text."""
    rn = _load_radio_news()
    raw = '<think>reasoning about the angle</think>\n{"title": "T", "body": "After think."}'
    assert rn._parse_rewrite(raw, "orig") == ("T", "After think.")


def test_rn_parse_rewrite_truncated_rescues():
    """Regression (live bug, 2026-07-28): a reasoning model burned the token
    budget on thinking and the JSON was cut mid-body — raw_decode failed and the
    raw `{"title": ..., "body": ` dumped into the slot. Rescue title + body by
    regex; the body has no closing quote, so match to end-of-text."""
    rn = _load_radio_news()
    raw = '{"title": "Argentina Debt Brightens", "body": "The IMF said debt hit 205 billion'
    assert rn._parse_rewrite(raw, "orig") == ("Argentina Debt Brightens",
                                              "The IMF said debt hit 205 billion")


def test_rn_parse_rewrite_truncated_before_body_no_dump():
    """Regression (live bug, 2026-07-28): truncation can cut BEFORE the body
    value opens (`"body":` with no quote) — no body to rescue. Must NOT dump the
    raw JSON into the slot; return the recovered title + empty body instead."""
    rn = _load_radio_news()
    raw = '{"title": "Oil prices plummet as conflict pauses", "body":'
    assert rn._parse_rewrite(raw, "orig") == ("Oil prices plummet as conflict pauses", "")


def test_rn_normalize_numbers_currency():
    """A leading-$ currency must read aloud as "<amount> dollars" — the gem is
    told to but flakes, so code guarantees it. Abbreviations expand, magnitude
    word stays, bare amounts get "dollars", and "dollars" never jams the next
    letter (word-boundary anchored). Trailing sentence punctuation untouched."""
    rn = _load_radio_news()
    assert rn._normalize_numbers("debt of $205 billion was") == "debt of 205 billion dollars was"
    assert rn._normalize_numbers("$3.2 million") == "3.2 million dollars"
    assert rn._normalize_numbers("cost was $5.") == "cost was 5 dollars."
    assert rn._normalize_numbers("$5bn deal") == "5 billion dollars deal"   # abbrev + no jam
    assert rn._normalize_numbers("raised $5m") == "raised 5 million dollars"
    assert rn._normalize_numbers("no money here") == "no money here"


def test_rn_prime_rewrites_calls_gateway_once_per_piece(monkeypatch):
    rn = _load_radio_news()
    monkeypatch.delenv("RADIO_REWRITE", raising=False)
    seen = []
    long_body = "broadcast line. " * 200  # >= MIN_WORDS so _rewrite accepts first try

    def fake_gateway(system, user, **kw):
        seen.append(user)
        return '{"title": "CUT", "body": "%s"}' % long_body

    monkeypatch.setattr(rn, "_gateway", fake_gateway)
    monkeypatch.setattr(rn, "_load_rewrite_gem", lambda: "GEM")
    p = {"title": "Raw", "content": "Long web body"}
    cache = rn._prime_rewrites([p, p])  # same object twice → deduped by id
    assert cache[id(p)][0] == "CUT"
    assert len(seen) == 1  # rewritten once, not twice
    assert "Long web body" in seen[0] and "Raw" in seen[0]


def test_rn_rewritten_flag_short_circuits(monkeypatch):
    """Antigravity seam: a piece already marked ``rewritten`` AND long enough
    (>= MIN_WORDS) is passed through verbatim — no gem load, no gateway call
    (zero re-pay). A short pre-rewritten cut is re-expanded (see next test)."""
    rn = _load_radio_news()
    monkeypatch.delenv("RADIO_REWRITE", raising=False)

    def boom(*a, **k):
        raise AssertionError("gateway must NOT be called for a long rewritten piece")

    monkeypatch.setattr(rn, "_gateway", boom)
    monkeypatch.setattr(rn, "_load_rewrite_gem", boom)
    long_cut = "already a broadcast cut. " * 50  # 250 words -> passes the floor
    p = {"title": "Pre-done", "content": long_cut, "rewritten": True}
    assert rn._prime_rewrites([p])[id(p)] == ("Pre-done", long_cut)


def test_rn_rewritten_short_piece_is_reexpanded(monkeypatch):
    """Safety net: a ``rewritten:true`` cut that came in UNDER the floor is
    re-expanded locally rather than shipped short (LLMs under-count words, so a
    thin Antigravity cut must not reach the slot)."""
    rn = _load_radio_news()
    monkeypatch.delenv("RADIO_REWRITE", raising=False)
    calls = []

    def fake_gateway(system, user, **kw):
        calls.append(user)
        return '{"title": "EXPANDED", "body": "%s"}' % ("expanded cut line. " * 200)

    monkeypatch.setattr(rn, "_gateway", fake_gateway)
    monkeypatch.setattr(rn, "_load_rewrite_gem", lambda: "GEM")
    p = {"title": "Thin", "content": "way too short cut", "rewritten": True}
    out = rn._prime_rewrites([p])[id(p)]
    assert out[0] == "EXPANDED"
    assert len(out[1].split()) >= rn.MIN_WORDS
    assert len(calls) == 1


def test_rn_rewrite_retries_for_length(monkeypatch):
    """_rewrite retries until the body clears MIN_WORDS, nudging longer on later
    attempts; returns the first >=floor body, not a short one."""
    rn = _load_radio_news()
    monkeypatch.delenv("RADIO_REWRITE", raising=False)
    seq = iter([
        '{"title": "T", "body": "%s"}' % ("short. " * 50),            # 50 words
        '{"title": "T", "body": "%s"}' % ("longer cut. " * 210),      # 420 -> >= floor
    ])

    monkeypatch.setattr(rn, "_gateway", lambda system, user, **kw: next(seq))
    monkeypatch.setattr(rn, "_load_rewrite_gem", lambda: "GEM")
    t, body = rn._rewrite("orig", "source article body", "GEM")
    assert len(body.split()) >= rn.MIN_WORDS


def test_rn_report_carries_rewritten_and_region(tmp_path, capsys):
    """CONVERT must carry ``rewritten`` + ``region`` through so a human-curated
    pick still short-circuits the rewrite at APPLY (regular lane = ultra-cheap)
    and SEA-lead placement survives the round-trip. Regression: cmd_report used
    to rebuild each result with only 6 fields, dropping both flags."""
    rn = _load_radio_news()
    handoff = tmp_path / "latest.json"
    handoff.write_text(json.dumps({
        "category": "global",
        "results": [
            {"title": "SEA lead", "url": "u1", "source": "Reuters", "date": "2026-07-28",
             "content": "cut", "words": 200, "region": "SEA", "rewritten": True},
            {"title": "plain", "url": "u2", "source": "AP", "date": "2026-07-28",
             "content": "cut2", "words": 210},  # no region/rewritten → defaults
        ],
        "slice_of_life": [],
    }))

    class _Args:
        path = str(handoff)

    rn.cmd_report(_Args())
    out = json.loads(capsys.readouterr().out)
    a, b = out["results"]
    assert a["region"] == "SEA" and a["rewritten"] is True
    assert b["region"] == "" and b["rewritten"] is False  # safe defaults


def test_rn_rewrite_off_env_passthrough(monkeypatch):
    """RADIO_REWRITE=off makes the whole pass a no-op → pytest stays offline."""
    rn = _load_radio_news()
    monkeypatch.setenv("RADIO_REWRITE", "off")

    def boom(*a, **k):
        raise AssertionError("gateway must NOT be called when RADIO_REWRITE=off")

    monkeypatch.setattr(rn, "_gateway", boom)
    monkeypatch.setattr(rn, "_load_rewrite_gem", boom)
    p = {"title": "Raw", "content": "Long web body"}
    assert rn._prime_rewrites([p])[id(p)] == ("Raw", "Long web body")


def test_rn_prime_rewrites_gateway_down_aborts(monkeypatch):
    """Gateway-availability failure inside the pre-pass exits nonzero (→502)
    BEFORE any doc read/write — the fail-fast guarantee that the doc is never
    half-filled. ``_gateway`` uses ``_fail_upstream`` = stderr + sys.exit(1)."""
    rn = _load_radio_news()
    monkeypatch.delenv("RADIO_REWRITE", raising=False)
    monkeypatch.setattr(rn, "_load_rewrite_gem", lambda: "GEM")
    monkeypatch.setattr(rn, "_omniroute_key", lambda: None)  # no key → _fail_upstream
    with pytest.raises(SystemExit):
        rn._prime_rewrites([{"title": "T", "content": "C"}])


def test_rn_load_rewrite_gem_slices_role_section(monkeypatch):
    """The gem loader returns only the ``## Role & Purpose`` body (up to the
    first ``\\n---\\n``) — the Ben voice, not the Notes/frontmatter."""
    rn = _load_radio_news()
    monkeypatch.setenv("RADIO_REWRITE_GEM", str(radio_news.REWRITE_GEM))
    gem = rn._load_rewrite_gem()
    assert "Ben" in gem
    assert "## Role & Purpose" not in gem  # heading itself sliced off
    assert "## Notes" not in gem            # trailing notes excluded


# ---------------------------------------------------------------- doc_format
# Publication-formatting pass: bold people names (glm-5 gem) + underline dates
# (regex). Pure helpers are tested directly; the gateway stays offline via
# monkeypatch, and the route rides radio_news.router (so _rn_client mounts it).


def _load_doc_format():
    import importlib.util

    for p in (newsroom.SCRIPTS / "doc_format.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "doc_format.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location("doc_format_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("doc_format.py not on a skill path yet (canonical writes pending)")


def _df_para(text, start):
    return {"paragraph": {"elements": [{"startIndex": start, "textRun": {"content": text}}]}}


def test_df_find_dates_variants():
    df = _load_doc_format()

    def got(s):
        return [m for _, _, m in df.find_dates(s)]

    assert got("filed January 5, 2026 in Bangkok") == ["January 5, 2026"]
    assert got("on 5 January the vote held") == ["5 January"]
    assert got("dated 2026-07-28 today") == ["2026-07-28", "today"]
    assert got("the Sep 3 2025 summit") == ["Sep 3 2025"]
    # lowercase month word as an ordinary verb must NOT match (case-sensitive +
    # a day number is required next to a capitalised month).
    assert got("they may go march soon") == []
    # relative time — broadcast bread-and-butter, underlined too. Case-blind so
    # a sentence-start capital still hits; determiner-gated so generics don't.
    assert got("Today, markets rallied. this week we saw gains.") == ["Today", "this week"]
    assert got("yesterday and last month") == ["yesterday", "last month"]
    assert got("next year, tomorrow, tonight") == ["next year", "tomorrow", "tonight"]
    assert got("this man and last resort") == []  # no unit -> no match


def test_df_name_spans_all_occurrences():
    df = _load_doc_format()
    parts = df._para_runs(_df_para("Prabowo met Prabowo again\n", 1))
    spans = df._name_spans_in(parts, "Prabowo")
    assert len(spans) == 2               # every occurrence styled
    assert spans[0] == (1, 8)            # abs [start, end) of first hit
    assert spans[1][0] == 13


def test_df_parse_names():
    df = _load_doc_format()
    assert df.parse_names('["Maris", "Prabowo"]') == ["Maris", "Prabowo"]
    assert df.parse_names('```json\n["A"]\n```') == ["A"]   # fence tolerated
    assert df.parse_names('["A", "A", "B"]') == ["A", "B"]  # deduped, order kept
    assert df.parse_names("not json") == []
    assert df.parse_names('{"not": "a list"}') == []


def test_df_plan_tab_tab_scoped_fields():
    """Every updateTextStyle carries the tab's tabId and the single field it
    sets (bold XOR underline) — a missing tabId writes into the wrong tab, a
    wrong ``fields`` mask clobbers unrelated styling."""
    df = _load_doc_format()
    meta = {"tab_id": "t.0",
            "paras": [_df_para("Prabowo met on January 5, 2026.\n", 1)]}
    reqs, underlined, bolded = df.plan_tab(meta, ["Prabowo"])
    assert "January 5, 2026" in underlined
    assert "Prabowo" in bolded
    for r in reqs:
        rng = r["updateTextStyle"]["range"]
        assert rng["tabId"] == "t.0"
    unders = [r for r in reqs if r["updateTextStyle"]["fields"] == "underline"]
    bolds = [r for r in reqs if r["updateTextStyle"]["fields"] == "bold"]
    assert len(unders) == 1 and unders[0]["updateTextStyle"]["textStyle"]["underline"] is True
    assert len(bolds) == 1 and bolds[0]["updateTextStyle"]["textStyle"]["bold"] is True
    assert bolds[0]["updateTextStyle"]["range"]["startIndex"] == 1  # 'Prabowo' at doc start


def test_df_format_doc_gateway_down_degrades(monkeypatch):
    """Gateway-down is NOT fail-fast here (unlike the rewrite): dates still get
    underlined, names are skipped, and the doc is written — the entity gem's own
    contract. Proves the date channel is independent of the name channel."""
    df = _load_doc_format()
    doc = {"tabs": [{
        "tabProperties": {"title": "AM", "tabId": "t.0"},
        "documentTab": {"body": {"content": [
            _df_para("Maris spoke on 2026-07-28 today\n", 1)]}},
    # NB "today" now also underlines (relative time) — kept in the fixture to
    # prove both the ISO date and the relative term survive the degrade path.
    }]}
    sent = []
    monkeypatch.setattr(df, "_get", lambda tok, url: doc)
    monkeypatch.setattr(df, "_post", lambda tok, url, body: sent.append(body) or {})
    monkeypatch.setattr(df, "_load_entities_gem", lambda: "GEM")

    def down(*a, **k):
        raise df.GatewayDown("gateway unreachable")

    monkeypatch.setattr(df, "_gateway", down)
    res = df.format_doc("tok", "DOC1")
    assert res["names_skipped"] is True
    assert res["bolded"] == []
    assert res["underlined"] == ["2026-07-28", "today"]
    # one batchUpdate carrying only the underline request
    assert len(sent) == 1
    reqs = sent[0]["requests"]
    assert all(r["updateTextStyle"]["fields"] == "underline" for r in reqs)


def test_df_load_entities_gem_slices_role_section(monkeypatch):
    df = _load_doc_format()
    monkeypatch.setenv("DOC_FORMAT_GEM", str(radio_news.FORMAT_GEM))
    gem = df._load_entities_gem()
    assert "person" in gem.lower()          # the role body
    assert "## Role & Purpose" not in gem   # heading sliced off
    assert "## Notes" not in gem            # trailing notes excluded


def test_df_format_route_argv(monkeypatch):
    c, calls, _ = _rn_client(monkeypatch, out=b'{"doc_id": "D", "tabs": [], '
                             b'"bolded": [], "underlined": [], "names_skipped": false}')
    r = c.post("/api/newsroom/format/apply", json={"doc_id": "D"})
    assert r.status_code == 200
    assert calls[0][1:] == [str(radio_news.DFORMAT), "--doc", "D"]
    # optional tab passes through
    c2, calls2, _ = _rn_client(monkeypatch, out=b'{"doc_id": "D", "tabs": [], '
                               b'"bolded": [], "underlined": [], "names_skipped": false}')
    c2.post("/api/newsroom/format/apply", json={"doc_id": "D", "tab": "AM"})
    assert calls2[0][1:] == [str(radio_news.DFORMAT), "--doc", "D", "--tab", "AM"]


def test_df_format_route_requires_doc_id(monkeypatch):
    c, _, _ = _rn_client(monkeypatch)
    assert c.post("/api/newsroom/format/apply", json={}).status_code == 400


# ---------------------------------------------------------------- rewrite (Ben + SEO)


def test_rewrite_success_and_prompt_assembly(monkeypatch):
    """POST /api/newsroom/rewrite loads Ben's gem (not news-producer), includes the
    v2 name overlay rule ([English(Thai)] / Thai fallback / source-only carve-out),
    instructs ~~date~~ underline markers, asks Ben for a {title, title_th, body} JSON
    object (which is reassembled into the EN:/TH: + body blob the panel sends), fires a
    separate SEO call with a Version-A-only override, and returns {"rewritten", "seo"}."""
    calls = []

    async def fake_zai_message(prompt: str, max_tokens: int = 400, system: str | None = None, model: str | None = None, timeout: float = 30.0) -> str:
        calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        if system:
            return "### Version A — AI Summary\nSummary text."
        return (
            '{"title": "Rail crash payout ordered", "title_th": "หัวข้อไทย", '
            '"body": "This is broadcast prose by **[Ben(เบ็น)]** mentioning **นายกฯ** on ~~July 15, 2026~~."}'
        )

    monkeypatch.setattr(newsroom.zai, "zai_message", fake_zai_message)
    app = FastAPI()
    app.include_router(newsroom.router)
    client = TestClient(app)

    r = client.post("/api/newsroom/rewrite", json={"text": "Prime Minister **นายกฯ** spoke on January 5, 2026."})
    assert r.status_code == 200
    data = r.json()
    assert "rewritten" in data and "seo" in data
    # JSON pieces reassembled into the sendable EN:/TH: + body blob:
    assert "EN: Rail crash payout ordered" in data["rewritten"]
    assert "TH: หัวข้อไทย" in data["rewritten"]
    assert "broadcast prose" in data["rewritten"]
    assert "Version A" in data["seo"]

    assert len(calls) == 2
    ben_call = [c for c in calls if not c["system"]][0]
    seo_call = [c for c in calls if c["system"]][0]

    # Ben call — JSON output schema + v2 name overlay rule:
    assert "editor of Thailand NOW" in ben_call["prompt"]           # Ben's gem body
    assert "single JSON object" in ben_call["prompt"]               # JSON output override
    assert '"title_th"' in ben_call["prompt"]                       # JSON schema keys
    assert "NAME OVERLAY RULE" in ben_call["prompt"]                # overlay heading present
    assert "[OfficialEnglish(Thai)]" in ben_call["prompt"]          # overlay format spec
    assert "NO transliteration" in ben_call["prompt"]               # no-guess rule
    assert "NARROW CARVE-OUT" in ben_call["prompt"]                 # source-only carve-out
    # ~~date~~ instruction:
    assert "~~…~~" in ben_call["prompt"]                            # date marker syntax
    assert "~~next month~~" in ben_call["prompt"]                   # relative-time example
    # v1 Thai-only blanket rule is GONE (replaced by overlay rule above).
    # "ORIGINAL THAI SCRIPT" still legitimately appears in the overlay rule for
    # titles/ranks, but the old blanket "never translate or transliterate any
    # PERSON'S NAME" sentence must NOT appear in v2:
    assert "leave every name and honorific in the ORIGINAL THAI SCRIPT" not in ben_call["prompt"]

    # SEO call asserts — Version A only (no Version B):
    assert "AI SEO Block" in seo_call["system"]                    # SEO gem body
    assert "Version A (40-60w summary)" in seo_call["system"]      # A-only override
    assert "Do NOT produce Version B" in seo_call["system"]        # B dropped


def test_rewrite_requires_text(monkeypatch):
    app = FastAPI()
    app.include_router(newsroom.router)
    client = TestClient(app)
    r = client.post("/api/newsroom/rewrite", json={"text": "   "})
    assert r.status_code == 400


def test_rewrite_empty_output_raises_502(monkeypatch):
    async def fake_empty(*a, **k):
        return ""

    monkeypatch.setattr(newsroom.zai, "zai_message", fake_empty)
    app = FastAPI()
    app.include_router(newsroom.router)
    client = TestClient(app)
    r = client.post("/api/newsroom/rewrite", json={"text": "Article text"})
    assert r.status_code == 502


def _load_nl_append():
    import importlib.util

    for p in (newsroom.SCRIPTS / "nl_append.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "nl_append.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location("nl_append_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("nl_append.py not found on a skill path")


def test_nl_append_emits_bold_and_underline_spans(monkeypatch):
    """nl_append unit test: **Name** produces bold span; ~~date~~ marker produces
    underline span (the marker path); DATE_RE backstop also fires on any date
    not already covered by a marker (idempotent if same span)."""
    nl = _load_nl_append()
    doc_posted = []

    def fake_nl_tab(tok, doc_id):
        return "t.0", 100

    def fake_api(method, url, tok, body=None):
        if method == "POST":
            doc_posted.append(body)
        return {}

    monkeypatch.setattr(nl, "find_tab", lambda *a, **k: ("t.0", 100))
    monkeypatch.setattr(nl, "api", fake_api)

    # Text with both **bold** name and ~~date~~ marker
    nl.append("tok", "DOC_ID",
              "Prime Minister **นายกฯ** spoke on ~~July 28, 2026~~.",
              dry=False)

    assert len(doc_posted) == 1
    reqs = doc_posted[0]["requests"]

    # insertText: both marker types stripped from the plain text
    ins = [r for r in reqs if "insertText" in r][0]
    assert ins["insertText"]["location"]["tabId"] == "t.0"
    assert "**" not in ins["insertText"]["text"]   # bold markers stripped
    assert "~~" not in ins["insertText"]["text"]    # underline markers stripped
    assert "นายกฯ" in ins["insertText"]["text"]     # bold content preserved
    assert "July 28, 2026" in ins["insertText"]["text"]  # underline content preserved

    # Bold span (field="bold", textStyle.bold=True)
    bold_reqs = [
        r for r in reqs
        if r.get("updateTextStyle", {}).get("fields") == "bold"
        and r["updateTextStyle"]["textStyle"].get("bold")
    ]
    assert len(bold_reqs) == 1

    # Underline span from ~~date~~ marker (field="underline", textStyle.underline=True)
    underline_reqs = [
        r for r in reqs
        if r.get("updateTextStyle", {}).get("fields") == "underline"
        and r["updateTextStyle"]["textStyle"].get("underline")
    ]
    assert len(underline_reqs) >= 1   # at least the marker-driven span


def test_nl_append_bold_after_underline_aligns(monkeypatch):
    """Regression: a **name** AFTER a ~~date~~ must still bold the name itself,
    not shifted letters. The old parse_bold→parse_underline chain computed bold
    offsets against the pre-underline-strip text, so a name following a date
    marker bolded the wrong span. parse_markers (single pass) fixes it."""
    nl = _load_nl_append()
    posted = []
    monkeypatch.setattr(nl, "find_tab", lambda tok, doc_id, *a: ("t.0", 100))
    monkeypatch.setattr(
        nl, "api",
        lambda method, url, tok, body=None: (posted.append(body), {})[1],
    )
    # date marker BEFORE the bold name — the ordering that exposed the bug
    nl.append("tok", "DOC", "On ~~July 28, 2026~~, **Anutin** spoke.", dry=False)
    reqs = posted[0]["requests"]
    ins = [r for r in reqs if "insertText" in r][0]["insertText"]["text"]
    bold_reqs = [
        r for r in reqs
        if r.get("updateTextStyle", {}).get("fields") == "bold"
        and r["updateTextStyle"]["textStyle"].get("bold")
    ]
    assert bold_reqs, "expected a bold span for the name"
    rng = bold_reqs[0]["updateTextStyle"]["range"]
    s, e = rng["startIndex"] - 100, rng["endIndex"] - 100
    assert ins[s:e] == "Anutin", f"bold span misaligned: selected {ins[s:e]!r}"


def test_nl_append_parse_underline_strips_markers(monkeypatch):
    """parse_underline isolated: strips ~~..~~ markers from the plain text and
    returns correct offsets into the stripped string (same math as parse_bold)."""
    nl = _load_nl_append()
    text = "Spoke on ~~July 28, 2026~~ in Bangkok."
    plain, ranges = nl.parse_underline(text)
    assert "~~" not in plain
    assert "July 28, 2026" in plain
    assert len(ranges) == 1
    s, e = ranges[0]
    assert plain[s:e] == "July 28, 2026"


def test_nl_append_marker_date_not_doubled_by_backstop(monkeypatch):
    """When Ben already wrapped a date in ~~…~~, the DATE_RE backstop deduplicates
    by start index so the span is emitted exactly once (not twice)."""
    nl = _load_nl_append()
    doc_posted = []

    monkeypatch.setattr(nl, "find_tab", lambda *a, **k: ("t.0", 100))
    monkeypatch.setattr(nl, "api",
                        lambda m, u, t, body=None: doc_posted.append(body) or {})

    # July 28 is both a ~~…~~ marker AND will be caught by DATE_RE as a backstop
    nl.append("tok", "DOC1", "Met on ~~July 28, 2026~~.", dry=False)

    reqs = doc_posted[0]["requests"]
    underline_reqs = [
        r for r in reqs
        if r.get("updateTextStyle", {}).get("fields") == "underline"
        and r["updateTextStyle"]["textStyle"].get("underline")
    ]
    # Dedup keeps it to 1 span (marker wins; backstop is suppressed for same start)
    assert len(underline_reqs) == 1


# ---------------------------------------------------------------- Phase 2 tests
# docfill.py + find_today_doc exact-match fix + fill_nl_slot + find_day_doc


def _load_nl_append_fresh():
    """Load nl_append with docfill importable (same scripts/ dir)."""
    import importlib.util
    import sys as _sys

    scripts_dir = newsroom.SCRIPTS
    # Ensure docfill is findable from the same dir as nl_append
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))

    for p in (scripts_dir / "nl_append.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "nl_append.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location("nl_append_fresh", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("nl_append.py not found")


def _load_docfill():
    import importlib.util
    import sys as _sys

    for p in (newsroom.SCRIPTS / "docfill.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "docfill.py"):
        if p.exists():
            if str(p.parent) not in _sys.path:
                _sys.path.insert(0, str(p.parent))
            spec = importlib.util.spec_from_file_location("docfill_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("docfill.py not found")


def test_phase2_find_today_doc_exact_match(monkeypatch):
    """find_today_doc MUST use exact name = 'NL & NWB DDMMYY' and return ONLY
    the 4-tab rundown doc — NOT the 'RUNDOWN NL-NWB' brief that the old
    'contains' query also matched."""
    nl = _load_nl_append_fresh()

    # Simulate Drive returning both the correct doc AND the wrong decoy.
    both_files = [
        {"id": "CORRECT_ID", "name": "NL & NWB 280726"},
        {"id": "WRONG_ID",   "name": "RUNDOWN NL-NWB 280726"},
    ]
    captured_query = []

    def fake_api(method, url, tok, body=None):
        captured_query.append(url)
        return {"files": both_files}

    monkeypatch.setattr(nl, "api", fake_api)

    doc_id, name = nl.find_today_doc("tok", "280726")
    # Must return the first result (exact match returns only the right doc first)
    assert doc_id == "CORRECT_ID"
    assert name == "NL & NWB 280726"

    # The query must use exact equality, not 'contains'
    assert captured_query, "no API call was made"
    q_url = captured_query[0]
    # The Drive API v3 query string is URL-encoded; `name = '...'` becomes
    # `name+%3D+%27...%27` or `name%20%3D%20%27...%27`. Check both forms.
    import urllib.parse as _up
    decoded_q = _up.unquote_plus(q_url)
    assert "name = " in decoded_q, (
        "find_today_doc must use exact `name = '...'` predicate, not `contains`; "
        f"got: {decoded_q!r}"
    )
    assert "contains" not in decoded_q, (
        "find_today_doc must NOT use `contains` (it matches the wrong RUNDOWN doc); "
        f"got: {decoded_q!r}"
    )


def test_phase2_docfill_parse_markers_single_pass():
    """parse_markers single-pass correctness: a **name** AFTER a ~~date~~
    must still bold the name at the right offset (the Phase-1 regression)."""
    df = _load_docfill()
    text = "On ~~July 28, 2026~~, **Anutin** spoke."
    plain, bolds, underlines = df.parse_markers(text)

    # Plain text has no markers
    assert "**" not in plain
    assert "~~" not in plain
    assert "July 28, 2026" in plain
    assert "Anutin" in plain

    # Bold span must cover exactly "Anutin"
    assert len(bolds) == 1
    s, e = bolds[0]
    assert plain[s:e] == "Anutin", (
        f"bold span misaligned: got {plain[s:e]!r} instead of 'Anutin'"
    )

    # Underline span must cover "July 28, 2026"
    assert len(underlines) == 1
    us, ue = underlines[0]
    assert plain[us:ue] == "July 28, 2026"


def test_phase2_build_fill_requests_structure():
    """build_fill_requests must produce [insertText, clear-bold, bold..., underline...]
    with correct tab scoping and aligned absolute indices."""
    df = _load_docfill()
    at = 50
    plain = "On July 28, Anutin spoke."
    # Simulate: bold [14, 20) = "Anutin", underline [3, 10) = "July 28"
    bolds = [(14, 20)]
    underlines = [(3, 10)]
    tab_id = "t.1"

    reqs = df.build_fill_requests(tab_id, at, plain, bolds, underlines)

    # Structure: insertText, clear-bold, bold spans, underline spans
    assert reqs[0]["insertText"]["location"] == {"index": at, "tabId": tab_id}
    assert reqs[0]["insertText"]["text"] == plain

    clear_bold = reqs[1]
    assert clear_bold["updateTextStyle"]["textStyle"]["bold"] is False
    assert clear_bold["updateTextStyle"]["range"]["startIndex"] == at
    assert clear_bold["updateTextStyle"]["range"]["endIndex"] == at + len(plain)

    bold_req = reqs[2]
    assert bold_req["updateTextStyle"]["textStyle"]["bold"] is True
    assert bold_req["updateTextStyle"]["range"]["startIndex"] == at + 14
    assert bold_req["updateTextStyle"]["range"]["endIndex"] == at + 20

    underline_req = reqs[3]
    assert underline_req["updateTextStyle"]["textStyle"]["underline"] is True
    assert underline_req["updateTextStyle"]["range"]["startIndex"] == at + 3
    assert underline_req["updateTextStyle"]["range"]["endIndex"] == at + 10


def test_phase2_fill_nl_slot_requests(monkeypatch):
    """fill_nl_slot end-to-end: builds correct batchUpdate with delete + insert
    + clear-bold + bold/underline spans. Regression: **name** after ~~date~~
    must bold the name, not shifted bytes."""
    nl = _load_nl_append_fresh()
    if not nl._DOCFILL:
        pytest.skip("docfill not importable in this environment")

    posted = []

    def fake_find_tab(tok, doc_id, title=None):
        return "t.0", 999  # (tab_id, end_unused)

    heading_resp = {
        "startIndex": 10,
        "endIndex": 25,
        "text": "3. Royal Birthday",
        "next_start": 200,
    }

    def fake_find_heading(api_get, doc_id, tab_id, match):
        return heading_resp

    def fake_api(method, url, tok, body=None):
        if method == "POST":
            posted.append(body)
        return {}

    monkeypatch.setattr(nl, "find_tab", fake_find_tab)
    monkeypatch.setattr(nl, "find_heading", fake_find_heading)
    monkeypatch.setattr(nl, "api", fake_api)

    # Text with ~~date~~ BEFORE **name** — the regression ordering
    nl.fill_nl_slot("tok", "DOC", "NL RUNDOWN", 3,
                    "On ~~July 28, 2026~~, **Anutin** spoke.", dry=False)

    assert posted, "expected a batchUpdate POST"
    reqs = posted[0]["requests"]

    # First request must be deleteContentRange for slot body [25, 200)
    dele = reqs[0]
    assert "deleteContentRange" in dele
    rng = dele["deleteContentRange"]["range"]
    assert rng["startIndex"] == 25   # h.endIndex
    assert rng["endIndex"] == 200    # h.next_start

    # Second request: insertText at h.endIndex
    ins = reqs[1]
    assert "insertText" in ins
    assert ins["insertText"]["location"]["index"] == 25
    plain = ins["insertText"]["text"]
    assert "**" not in plain
    assert "Anutin" in plain

    # Bold span must cover "Anutin" (not shifted by the preceding date marker)
    bold_reqs = [
        r for r in reqs
        if r.get("updateTextStyle", {}).get("fields") == "bold"
        and r["updateTextStyle"]["textStyle"].get("bold")
    ]
    assert bold_reqs, "expected at least one bold span"
    rng = bold_reqs[0]["updateTextStyle"]["range"]
    s = rng["startIndex"] - 25   # subtract insert offset to get plain index
    e = rng["endIndex"] - 25
    assert plain[s:e] == "Anutin", (
        f"bold span misaligned after ~~date~~: got {plain[s:e]!r}"
    )


def test_phase2_find_day_doc_weekday_vs_weekend(monkeypatch):
    """find_day_doc picks Weekend Script on Sat/Sun, Weekday Script Mon-Fri."""
    radio = _load_radio()

    def fake_find_month_folder(*a, **k):
        return ("FOLDER_ID", "202607 July")

    files_in_folder = [
        {"id": "WD_ID", "name": "20260727_Weekday Script"},   # 2026-07-27 = Mon
        {"id": "WE_ID", "name": "20260726_Weekend Script"},   # 2026-07-26 = Sun
    ]

    def fake_api(method, url, params=None, body=None):
        return {"files": files_in_folder}

    monkeypatch.setattr(radio, "find_month_folder", fake_find_month_folder)
    monkeypatch.setattr(radio, "_api", fake_api)

    # Monday 2026-07-27 → Weekday Script
    doc_id, name = radio.find_day_doc("PARENT", "20260727")
    assert doc_id == "WD_ID"
    assert "Weekday" in name

    # Sunday 2026-07-26 → Weekend Script
    doc_id, name = radio.find_day_doc("PARENT", "20260726")
    assert doc_id == "WE_ID"
    assert "Weekend" in name

    # Saturday 2026-07-25 — add to files list
    files_in_folder.append({"id": "WE_ID2", "name": "20260725_Weekend Script"})
    doc_id, name = radio.find_day_doc("PARENT", "20260725")
    assert doc_id == "WE_ID2"
    assert "Weekend" in name


def test_phase2_api_fill_nl_argv(monkeypatch):
    """POST /api/newsroom/fill builds the right argv; 400 on missing/bad slot."""
    c, calls = _client(monkeypatch, out=b'{"filled": true}')

    # Happy path
    r = c.post("/api/newsroom/fill",
               json={"text": "Hello **World**.", "tab": "NL", "slot": 3})
    assert r.status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("nl_append.py")
    assert argv[2] == "fill"
    assert "--tab" in argv and argv[argv.index("--tab") + 1] == "NL"
    assert "--slot" in argv and argv[argv.index("--slot") + 1] == "3"
    assert "--today" in argv
    assert "--text" in argv

    # Default tab is NL when omitted
    calls.clear()
    r2 = c.post("/api/newsroom/fill", json={"text": "Script.", "slot": 1})
    assert r2.status_code == 200
    argv2 = calls[0]
    assert argv2[argv2.index("--tab") + 1] == "NL"

    # explicit doc_id overrides --today
    calls.clear()
    r3 = c.post("/api/newsroom/fill",
                json={"text": "T", "slot": 2, "doc_id": "DOCXYZ"})
    assert r3.status_code == 200
    argv3 = calls[0]
    assert "--doc" in argv3 and argv3[argv3.index("--doc") + 1] == "DOCXYZ"
    assert "--today" not in argv3

    # Missing slot → 400
    assert c.post("/api/newsroom/fill", json={"text": "T"}).status_code == 400
    # Non-int slot → 400
    assert c.post("/api/newsroom/fill", json={"text": "T", "slot": "x"}).status_code == 400
    # Missing text → 400
    assert c.post("/api/newsroom/fill", json={"slot": 1}).status_code == 400


def test_phase2_api_radio_fill_argv(monkeypatch):
    """POST /api/newsroom/radio/fill builds the right argv; 400 on missing fields."""
    c, calls = _client(monkeypatch, out=b'{"filled": true}')

    body = {
        "text": "Broadcast copy.",
        "year": 2026, "month": 7, "day": 28,
        "section": "AM", "block": "NATIONAL", "slot": 2,
    }
    r = c.post("/api/newsroom/radio/fill", json=body)
    assert r.status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("radio.py")
    assert argv[2] == "fill"
    assert "--year" in argv and argv[argv.index("--year") + 1] == "2026"
    assert "--month" in argv and argv[argv.index("--month") + 1] == "7"
    assert "--day" in argv and argv[argv.index("--day") + 1] == "28"
    assert "--section" in argv and argv[argv.index("--section") + 1] == "AM"
    assert "--block" in argv and argv[argv.index("--block") + 1] == "NATIONAL"
    assert "--slot" in argv and argv[argv.index("--slot") + 1] == "2"
    assert "--text" in argv

    # Missing required fields → 400
    for drop in ("year", "month", "day", "section", "block", "slot"):
        bad = {k: v for k, v in body.items() if k != drop}
        assert c.post("/api/newsroom/radio/fill", json=bad).status_code == 400, (
            f"expected 400 when {drop!r} is missing"
        )
    # Missing text → 400
    no_text = {k: v for k, v in body.items() if k != "text"}
    assert c.post("/api/newsroom/radio/fill", json=no_text).status_code == 400



# ---------------------------------------------------------------- infographics


_INFO_SCRIPT = (
    "Intro paragraph with no data.\n\n"
    "The country welcomed 35.2 million arrivals, generating 1.2 trillion baht.\n\n"
    "\"A quote paragraph,\" said **Somchai Pattana**.\n\n"
    "A soft closing paragraph."
)


def test_annotate_infographics_never_touches_the_news():
    """The prose is copied verbatim and paragraph 1 is unannotatable — the two ways
    the LLM-reproduces-the-script version corrupted real scripts (block above the
    lede; lede deleted outright)."""
    from app.newsroom import _annotate_infographics

    picks = [
        {"paragraph": 2, "headline": "Arrivals", "why": "w", "intake": "i",
         "facts": "35.2 million, 1.2 trillion baht"},
        {"paragraph": 1, "headline": "LEDE", "why": "w", "intake": "i", "facts": "f"},
        {"paragraph": 99, "headline": "OOB", "why": "w", "intake": "i", "facts": "f"},
        {"paragraph": "bad", "headline": "NAN", "why": "w", "intake": "i", "facts": "f"},
    ]
    out = _annotate_infographics(_INFO_SCRIPT, picks)

    # every original paragraph survives, in order, byte-for-byte
    pos = -1
    for para in _INFO_SCRIPT.split("\n\n"):
        i = out.find(para)
        assert i > pos, "paragraph dropped or reordered: %r" % para
        pos = i
    # the lede is still first — no block may precede it
    assert out.startswith("Intro paragraph with no data.")
    # out-of-range / non-numeric / paragraph-1 picks are all discarded
    assert "LEDE" not in out and "OOB" not in out and "NAN" not in out
    # the one valid pick landed directly above its paragraph, figures verbatim
    assert out.index("----- INFOGRAPHIC: Arrivals") < out.index("The country welcomed")
    assert "News fact + data point:  35.2 million, 1.2 trillion baht" in out


def test_annotate_infographics_empty_picks_is_identity():
    """Zero qualifying paragraphs is a valid result — the script must come back unchanged."""
    from app.newsroom import _annotate_infographics
    assert _annotate_infographics(_INFO_SCRIPT, []) == _INFO_SCRIPT


# ---------------------------------------------------------------- newsline reports


def _load_newsline_reports():
    import importlib.util

    for p in (
        Path(__file__).parent.parent / "app" / "newsline_reports.py",
        newsroom.SCRIPTS / "newsline_reports.py",
        Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "newsline_reports.py",
    ):
        if p.exists():
            spec = importlib.util.spec_from_file_location("newsline_reports_mod", p)
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)
            return mod
    pytest.skip("newsline_reports.py not found")


def test_nl_reports_thai_digits_and_dates():
    nl_rep = _load_newsline_reports()
    from datetime import date
    assert nl_rep.to_thai_digits(0) == "๐"
    assert nl_rep.to_thai_digits(123456789) == "๑๒๓๔๕๖๗๘๙"
    assert nl_rep.be_year(2026) == 2569
    assert nl_rep.format_thai_western(date(2026, 8, 21)) == "21 สิงหาคม 2569"
    assert nl_rep.format_thai_numerals(date(2026, 8, 21)) == "๒๑ สิงหาคม ๒๕๖๙"


def test_nl_reports_fy_be_derivation():
    nl_rep = _load_newsline_reports()
    # Before Oct 1 (Jan-Sep) -> CE + 543
    assert nl_rep.fy_be(2026, 1) == 2569
    assert nl_rep.fy_be(2026, 8) == 2569
    assert nl_rep.fy_be(2026, 9) == 2569
    # On or after Oct 1 (Oct-Dec) -> CE + 543 + 1
    assert nl_rep.fy_be(2026, 10) == 2570
    assert nl_rep.fy_be(2026, 11) == 2570
    assert nl_rep.fy_be(2026, 12) == 2570


def test_nl_reports_filename_period_prefix():
    nl_rep = _load_newsline_reports()
    from datetime import date
    # Period 11, August 2026
    c_name = nl_rep.cover_doc_name(11, date(2026, 8, 1), date(2026, 8, 31))
    l_name = nl_rep.log_doc_name(11, date(2026, 8, 1), date(2026, 8, 31))
    assert c_name == "11 ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ.docx"
    assert l_name == "11 รายงานผลการปฏิบัติงาน สิงหาคม 2569.docx"
    assert c_name.startswith("11 ")
    assert l_name.startswith("11 ")

    # String period and whitespace strip
    c_name_str = nl_rep.cover_doc_name(" 5 ", date(2026, 8, 1), date(2026, 8, 31))
    l_name_str = nl_rep.log_doc_name(" 5 ", date(2026, 8, 1), date(2026, 8, 31))
    assert c_name_str == "5 ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ.docx"
    assert l_name_str == "5 รายงานผลการปฏิบัติงาน สิงหาคม 2569.docx"


def test_nl_reports_build_plan_aug2026():
    nl_rep = _load_newsline_reports()
    from datetime import date
    plan = nl_rep.build_plan(5, date(2026, 8, 1), date(2026, 8, 31))
    assert plan["period"] == "5"
    assert plan["fy_be"] == 2569
    assert plan["weekday_count"] == 21
    assert plan["cover_filename"] == "5 ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ.docx"
    assert plan["log_filename"] == "5 รายงานผลการปฏิบัติงาน สิงหาคม 2569.docx"
    # First Mon-Fri in Aug 2026 is Mon Aug 3 (Aug 1=Sat, Aug 2=Sun)
    assert plan["rows"][0] == "๓ สิงหาคม ๒๕๖๙  รายการ NEWSLINE"
    assert plan["rows"][-1] == "๓๑ สิงหาคม ๒๕๖๙  รายการ NEWSLINE"
    assert len(plan["rows"]) == 21


def test_nl_reports_preview_makes_no_network_writes(monkeypatch, capsys):
    nl_rep = _load_newsline_reports()

    def boom(*a, **k):
        raise AssertionError("preview touched a write path")

    monkeypatch.setattr(nl_rep, "google_token", boom)
    monkeypatch.setattr(nl_rep, "create_folder", boom)
    monkeypatch.setattr(nl_rep, "copy_file", boom)
    monkeypatch.setattr(nl_rep, "upload_media", boom)
    monkeypatch.setattr(
        nl_rep, "find_or_create_fy_folder",
        lambda root_id, fy, dry=False: ("FY_FOLDER_ID", f"งบประมาณ {fy}", False),
    )

    nl_rep.main(["--period", "5", "--start", "2026-08-01", "--end", "2026-08-31", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["fy_be"] == 2569
    assert out["period"] == "5"
    assert out["weekday_count"] == 21
    assert len(out["rows"]) == 21
    assert out["created"] == []
    assert out["cover"]["name"] == "5 ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ.docx"
    assert out["log"]["name"] == "5 รายงานผลการปฏิบัติงาน สิงหาคม 2569.docx"


def test_nl_reports_generate_idempotent_skips_duplicates(monkeypatch, capsys):
    nl_rep = _load_newsline_reports()

    monkeypatch.setattr(
        nl_rep, "find_or_create_fy_folder",
        lambda root_id, fy, dry=False: ("FY_ID", f"งบประมาณ {fy}", False),
    )
    # Both cover and log already exist in the folder
    monkeypatch.setattr(
        nl_rep, "find_existing_files",
        lambda fid: [
            {"id": "c1", "name": "5 ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ.docx", "webViewLink": "http://cover"},
            {"id": "l1", "name": "5 รายงานผลการปฏิบัติงาน สิงหาคม 2569.docx", "webViewLink": "http://log"},
        ],
    )

    def boom(*a, **k):
        raise AssertionError("idempotent generate attempted to write")

    monkeypatch.setattr(nl_rep, "copy_file", boom)
    monkeypatch.setattr(nl_rep, "upload_media", boom)

    nl_rep.main(["--period", "5", "--start", "2026-08-01", "--end", "2026-08-31", "generate"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is False
    assert out["idempotent"] is True
    assert out["cover"] == {"id": "c1", "name": "5 ใบรายงานผลการปฏิบัติงาน แบบ QR Code สิงหาคม 2569 ณอรรฆย์ โรจนสุวรรณ.docx", "url": "http://cover"}
    assert out["log"] == {"id": "l1", "name": "5 รายงานผลการปฏิบัติงาน สิงหาคม 2569.docx", "url": "http://log"}
    assert len(out["skipped"]) == 2
    assert out["created"] == []


def test_nl_reports_fill_cover_xml():
    nl_rep = _load_newsline_reports()
    from datetime import date
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p>
          <w:r><w:t>รายงานผลการปฏิบัติงานประจำ งวดที่....</w:t></w:r>
          <w:r><w:t>X</w:t></w:r>
          <w:r><w:t>......ระหว่างวันที่....</w:t></w:r>
        </w:p>
      </w:body>
    </w:document>""".encode("utf-8")
    filled = nl_rep.fill_cover_xml(sample_xml, 11, date(2026, 8, 1), date(2026, 8, 31)).decode("utf-8")
    assert "รายงานผลการปฏิบัติงานประจำ งวดที่....11......ระหว่างวันที่....1 สิงหาคม 2569 – 31 สิงหาคม 2569....." in filled


def test_nl_reports_fill_log_xml():
    nl_rep = _load_newsline_reports()
    from datetime import date
    sample_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:r><w:t>รายงานผลการปฏิบัติงานงวดที่ X ปีงบประมาณ 256X</w:t></w:r></w:p>
        <w:p><w:r><w:t>ชื่อ-สกุลนายณอรรฆย์ โรจนสุวรรณ</w:t></w:r></w:p>
        <w:p><w:r><w:t>ตั้งแต่วันที่ ๑ ตุลาคม - ๒๐ ตุลาคม ๒๕๖๘</w:t></w:r></w:p>
        <w:p><w:r><w:t>วันที่ ๑ ตุลาคม ๒๕๖๘</w:t></w:r></w:p>
        <w:p><w:r><w:t>รายการ NEWSLINE</w:t></w:r></w:p>
      </w:body>
    </w:document>""".encode("utf-8")
    weekdays = [date(2026, 8, 3), date(2026, 8, 4)]
    filled = nl_rep.fill_log_xml(sample_xml, 11, date(2026, 8, 1), date(2026, 8, 31), weekdays).decode("utf-8")
    assert "รายงานผลการปฏิบัติงานงวดที่ ๑๑ ปีงบประมาณ ๒๕๖๙" in filled
    assert "ตั้งแต่วันที่ ๑ สิงหาคม ๒๕๖๙ - ๓๑ สิงหาคม ๒๕๖๙" in filled
    assert "วันที่ ๓ สิงหาคม ๒๕๖๙" in filled
    assert "วันที่ ๔ สิงหาคม ๒๕๖๙" in filled
    assert "รายการ NEWSLINE" in filled


def test_nl_reports_preview_and_generate_routes(monkeypatch):
    c, calls = _client(monkeypatch, out=b'{"dry_run": true}')
    r_prev = c.post(
        "/api/newsroom/newsline-reports/preview",
        json={"period": 5, "start": "2026-08-01", "end": "2026-08-31"},
    )
    assert r_prev.status_code == 200
    argv = calls[0]
    assert argv[0] == "python3"
    assert argv[1].endswith("newsline_reports.py")
    assert argv[2:8] == ["--period", "5", "--start", "2026-08-01", "--end", "2026-08-31"]
    assert "--dry-run" in argv

    c2, calls2 = _client(monkeypatch, out=b'{"created": []}')
    r_gen = c2.post(
        "/api/newsroom/newsline-reports/generate",
        json={"period": "11", "start": "2026-08-01", "end": "2026-08-31"},
    )
    assert r_gen.status_code == 200
    argv2 = calls2[0]
    assert "--dry-run" not in argv2
    assert argv2[-1] == "generate"

    # Missing fields -> 400
    assert c.post("/api/newsroom/newsline-reports/preview", json={"start": "2026-08-01", "end": "2026-08-31"}).status_code == 400
    assert c.post("/api/newsroom/newsline-reports/preview", json={"period": 5, "end": "2026-08-31"}).status_code == 400
    assert c.post("/api/newsroom/newsline-reports/preview", json={"period": 5, "start": "2026-08-01"}).status_code == 400

