"""Newsroom panel backend — argv construction + error surfacing.

The skill scripts are the contract, so ``_run`` is monkeypatched (no real
newstank / Google Docs): each test captures the argv the route would exec and
feeds back a canned (rc, stdout, stderr).
"""

import json

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
