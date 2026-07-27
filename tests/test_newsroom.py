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

from app import newsroom


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
