"""Tests for the Ahrefs import + issue backlog (Phase 1) and the alt-text
suggester/apply flow (Phase 2). Backlog file is redirected to tmp; WP faked."""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import thailandnow
from app.main import app

SITE = "https://www.thailandnow.in.th"

AHREFS_CSV = """PR,URL,Title,Content type,Is rendered page,HTTP status code,Size (bytes),Loading time (ms),No. of IMG inlinks
0,https://www.thailandnow.in.th/wp-content/uploads/2026/04/MFA-briefing-scaled.png,,image/png,false,200,6999215,1236,20
0,https://www.thailandnow.in.th/wp-content/uploads/2026/09/AI-drag-2-scaled.png,,image/png,false,200,5715432,987,29
"""


@pytest.fixture(autouse=True)
def _tmp_backlog(monkeypatch, tmp_path):
    monkeypatch.setattr(thailandnow, "SEO_BACKLOG_FILE", tmp_path / "seo_backlog.json")


@pytest.fixture
def client():
    return TestClient(app)


def test_import_parses_and_dedupes(client):
    r = client.post("/api/thailandnow/seo/import",
                    json={"report_type": "image_file_size", "csv_text": AHREFS_CSV})
    d = r.json()
    assert r.status_code == 200
    assert d["rows"] == 2 and d["new"] == 2 and d["total_open"] == 2
    assert d["open_by_type"] == {"image_file_size": 2}

    # re-import same rows → updated, not duplicated
    r2 = client.post("/api/thailandnow/seo/import",
                     json={"report_type": "image_file_size", "csv_text": AHREFS_CSV})
    d2 = r2.json()
    assert d2["new"] == 0 and d2["updated"] == 2 and d2["total_open"] == 2

    bl = client.get("/api/thailandnow/seo/backlog").json()
    assert bl["total_open"] == 2
    assert set(bl["by_type"]) == {"image_file_size"}
    assert bl["issues"][0]["inlinks"] in (20, 29)


def test_import_reopens_fixed(client):
    client.post("/api/thailandnow/seo/import",
                json={"report_type": "image_file_size", "csv_text": AHREFS_CSV})
    bl = client.get("/api/thailandnow/seo/backlog").json()
    key = None
    # find the key via the raw backlog state
    import pathlib
    state = json.loads(pathlib.Path(thailandnow.SEO_BACKLOG_FILE).read_text())
    for k, v in state["issues"].items():
        v["status"] = "fixed"
        key = k
    pathlib.Path(thailandnow.SEO_BACKLOG_FILE).write_text(json.dumps(state))
    r = client.post("/api/thailandnow/seo/import",
                    json={"report_type": "image_file_size", "csv_text": AHREFS_CSV})
    d = r.json()
    assert d["reopened_fixed"] == 2 and d["total_open"] == 2
    assert key


def test_set_status_dismiss_removes_from_open(client):
    client.post("/api/thailandnow/seo/import",
                json={"report_type": "image_file_size", "csv_text": AHREFS_CSV})
    state = json.loads(pathlib.Path(thailandnow.SEO_BACKLOG_FILE).read_text())
    key = next(iter(state["issues"]))
    r = client.post("/api/thailandnow/seo/backlog/set-status",
                    json={"key": key, "status": "dismissed"})
    assert r.json()["ok"] is True
    bl = client.get("/api/thailandnow/seo/backlog").json()
    assert bl["total_open"] == 1


def test_import_rejects_empty_csv(client):
    r = client.post("/api/thailandnow/seo/import",
                    json={"report_type": "image_file_size", "csv_text": ""})
    assert r.status_code == 400


# ------------------------------------------------------------- alt text ----

class _FakeRecWP:
    def __init__(self, contents):
        self.contents = dict(contents)
        self.puts = []

    async def __call__(self, method, path, params=None, json_body=None):
        parts = path.strip("/").split("/")
        if parts[0] == "media":
            mid = int(parts[1])
            return {"id": mid}
        pid = int(parts[1])
        if method == "GET":
            return {"id": pid, "content": {"raw": self.contents.get(pid, "")}}
        self.contents[pid] = json_body["content"]
        self.puts.append((pid, json_body["content"]))
        return {"id": pid}


def test_alt_apply_record_and_media(client, monkeypatch):
    wp = _FakeRecWP({55: '<p><img src="https://x/a.png"> <img src="https://x/b.png" alt=""></p>'})
    async def resolve(pid):
        return "posts"

    monkeypatch.setattr(thailandnow, "_wp", wp)
    monkeypatch.setattr(thailandnow, "_wp_resolve_rest_base", resolve)
    r = client.post("/api/thailandnow/seo/alt/apply", json={"items": [
        {"record_id": 55, "src": "https://x/a.png", "alt": "A dish"},
        {"record_id": 55, "src": "https://x/b.png", "alt": "Another dish"},
        {"media_id": 777, "alt": "Cover image"},
    ]})
    d = r.json()
    assert d["applied"] == 3 and d["failed"] == 0, d
    assert d["results"][0]["matches"] == 1
    assert 'alt="A dish"' in wp.contents[55]
    assert 'alt="Another dish"' in wp.contents[55]


def test_alt_apply_noop_when_src_missing(client, monkeypatch):
    wp = _FakeRecWP({55: "<p>no images</p>"})
    async def resolve(pid):
        return "posts"

    monkeypatch.setattr(thailandnow, "_wp", wp)
    monkeypatch.setattr(thailandnow, "_wp_resolve_rest_base", resolve)
    r = client.post("/api/thailandnow/seo/alt/apply", json={"items": [
        {"record_id": 55, "src": "https://x/ghost.png", "alt": "ghost"}]})
    d = r.json()
    assert d["applied"] == 1 and d["results"][0].get("noop") is True
