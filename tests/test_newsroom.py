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
    assert argv[4:6] == ["--author", "Chompatsorn"]
    assert "--date" not in argv


def test_queue_passes_date_and_author(monkeypatch):
    c, calls = _client(monkeypatch, out=b"{}")
    assert c.get("/api/newsroom/queue?date=2026-07-21&author=all").status_code == 200
    argv = calls[0]
    assert ["--author", "all"] == argv[4:6]
    assert ["--date", "2026-07-21"] == argv[6:8]


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

    for p in (newsroom.SCRIPTS / "radio.py",
              Path.home() / ".claude" / "skills" / "newsroom" / "scripts" / "radio.py"):
        if p.exists():
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
    assert out["to_create"][0] == {"name": "202608 August", "kind": "sheet"}
    assert len(out["to_create"]) == 32


def test_radio_dry_run_skips_existing(monkeypatch, capsys):
    radio = _load_radio()
    monkeypatch.setattr(radio, "google_token", lambda *a, **k: pytest.fail("net"))
    monkeypatch.setattr(radio, "copy_file", lambda *a, **k: pytest.fail("write"))
    monkeypatch.setattr(radio, "find_month_folder",
                        lambda *a, **k: ("F", "202608 August"))
    # the sheet already exists → idempotent skip.
    monkeypatch.setattr(radio, "existing_names",
                        lambda *a, **k: {"202608 August"})
    radio.main(["--year", "2026", "--month", "8", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["to_create"] == 31
    assert out["counts"]["skipped"] == 1


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
