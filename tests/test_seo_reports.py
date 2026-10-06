"""SEO report triage: import/backlog/registry + image-size handler plan/apply.

Everything rides fakes — `thailandnow._wp` / `._wp_list_all` monkeypatched,
image downloads + uploads via httpx.MockTransport. Nothing touches the real
site; backlog/session/backups live in tmp_path.
"""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.thailand_now import scout as thailandnow, seo_reports
from app.main import app


@pytest.fixture(autouse=True)
def _isolated_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(seo_reports, "BACKLOG_FILE", tmp_path / "backlog.json")
    monkeypatch.setattr(seo_reports, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(seo_reports, "BACKUPS_DIR", tmp_path / "backups")
    monkeypatch.setattr(seo_reports, "CHECKPOINT_FILE", tmp_path / "checkpoint.json")


@pytest.fixture
def client():
    return TestClient(app)


CSV = (
    "PR,URL,Title,Content type,Is rendered page,HTTP status code,Size (bytes),Loading time (ms),No. of IMG inlinks\n"
    "0,https://www.thailandnow.in.th/wp-content/uploads/2021/05/chut-thai-ayutthaya-ruins.png,,image/png,false,200,1400000,900,1\n"
    "1,https://www.thailandnow.in.th/wp-content/uploads/2021/05/pom-phet-diamond-fortress-ayutthaya.png,,image/png,false,200,1020000,800,0\n"
)
CSV_NAME = "thailandnow_03-oct-2026_image-file-size-too-l_2026-10-03_10-40-39.csv"

IMAGE_URL = "https://www.thailandnow.in.th/wp-content/uploads/2021/05/chut-thai-ayutthaya-ruins.png"
NEW_URL = "https://www.thailandnow.in.th/wp-content/uploads/2026/10/chut-thai-ayutthaya-ruins-682x1024.jpg"
POST_ID = 4250

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
)

POST_RAW = (
    "<!-- wp:image -->\n<figure class=\"wp-block-image size-full\">"
    f"<img src=\"{IMAGE_URL.rsplit('.', 1)[0]}-682x1024.png\" alt=\"ruins\"/></figure>\n"
    "<!-- /wp:image -->\n<p>hello</p>"
)

MEDIA = [
    {"id": 999, "source_url": IMAGE_URL,
     "media_details": {"filesize": 1400000, "width": 682, "height": 1024,
                       "sizes": {"large": {"width": 500, "height": 700, "filesize": 200000}}}},
]

POSTS = [
    {"id": POST_ID, "link": "https://www.thailandnow.in.th/life-society/beyond-the-surface-of-ayudhya/",
     "title": {"rendered": "Beyond the Surface of Ayudhya"},
     "content": {"rendered": f"<img src=\"{IMAGE_URL.rsplit('.', 1)[0]}-682x1024.png\">"},
     "featured_media": 0},
    {"id": 4000, "link": "https://www.thailandnow.in.th/life-society/some-other-page/",
     "title": {"rendered": "Other"}, "content": {"rendered": "<p>nothing</p>"},
     "featured_media": 999},
]


def _fake_wp(state):
    """async _wp replacement dispatching on (method, path)."""

    async def fake_wp(method, path, params=None, json_body=None):
        page = (params or {}).get("page", 1)
        if method == "GET" and re.match(rf"/posts/{POST_ID}$", path):
            return {"id": POST_ID, "link": "https://x/",
                    "content": {"raw": state.get("raw", POST_RAW),
                                "rendered": state.get("raw", POST_RAW)}}
        if method == "GET" and path.startswith("/media/"):
            return MEDIA[0]
        if method == "PUT" and path.startswith("/posts/"):
            state["put_calls"] = state.get("put_calls", []) + [json_body]
            state["raw"] = json_body["content"]
            return {"id": POST_ID}
        if method == "GET" and path == "/media":
            return MEDIA if page == 1 else []
        if method == "GET" and path == "/posts":
            return POSTS if page == 1 else []
        if method == "GET" and path in ("/pages", "/event"):
            return []
        raise AssertionError(f"unexpected _wp call: {method} {path}")

    return fake_wp


async def _fake_list_all(endpoint, fields):
    if endpoint == "/media":
        return MEDIA
    if endpoint == "/posts":
        return POSTS
    return []


async def _fake_paced(tn, endpoint, fields, key, **kw):
    if endpoint == "/media":
        return MEDIA
    if endpoint == "/posts":
        return POSTS
    return []


@pytest.fixture
def _fakes(monkeypatch):
    state = {}
    monkeypatch.setattr(thailandnow, "_wp", _fake_wp(state))
    monkeypatch.setattr(seo_reports, "wp_list_all_paced", _fake_paced)
    return state


def _mock_http(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith(".png"):
            return httpx.Response(200, content=PNG_1PX, headers={"content-type": "image/png"})
        if request.method == "POST" and request.url.path.endswith("/media"):
            return httpx.Response(201, json={"id": 555, "source_url": NEW_URL})
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(**kw):
        kw["transport"] = transport
        return real(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_import_derives_type_dedupes_and_reopens(client):
    r = client.post("/api/thailandnow/seo/reports/import",
                    json={"csv_text": CSV, "filename": CSV_NAME}).json()
    assert r["report_type"] == "image-file-size-too-l"
    assert r["handler"] == "image-size"
    assert r["rows"] == 2 and r["new"] == 2 and r["total_open"] == 2

    # mark one fixed, then re-import the same crawl -> fixed one REOPENS
    r2 = client.post("/api/thailandnow/seo/reports/import",
                     json={"csv_text": CSV, "filename": CSV_NAME}).json()
    assert r2["new"] == 0 and r2["updated"] == 2
    bl = client.get("/api/thailandnow/seo/reports/backlog").json()
    key = next(x["key"] for x in bl["issues"] if x["url"] == IMAGE_URL)
    client.post("/api/thailandnow/seo/reports/set-status", json={"key": key, "status": "fixed"})
    r3 = client.post("/api/thailandnow/seo/reports/import",
                     json={"csv_text": CSV, "filename": CSV_NAME}).json()
    assert r3["total_open"] == 2  # reopened
    bl = client.get("/api/thailandnow/seo/reports/backlog").json()
    assert len(bl["history"]) == 3


def test_import_unknown_slug_tracked_without_handler(client):
    r = client.post("/api/thailandnow/seo/reports/import", json={
        "csv_text": "URL\nhttps://www.thailandnow.in.th/x\n",
        "filename": "site_01-jan-2026_something-else_2026-01-01_00-00-00.csv"}).json()
    assert r["report_type"] == "something-else"
    assert r["handler"] is None


def test_set_status_validation(client):
    client.post("/api/thailandnow/seo/reports/import", json={"csv_text": CSV, "filename": CSV_NAME})
    bl = client.get("/api/thailandnow/seo/reports/backlog").json()
    key = bl["issues"][0]["key"]
    assert client.post("/api/thailandnow/seo/reports/set-status",
                       json={"key": key, "status": "bogus"}).status_code == 400
    assert client.post("/api/thailandnow/seo/reports/set-status",
                       json={"key": "nope", "status": "fixed"}).status_code == 404


def test_enrich_classifies(client, _fakes):
    client.post("/api/thailandnow/seo/reports/import", json={"csv_text": CSV, "filename": CSV_NAME})
    r = client.post("/api/thailandnow/seo/reports/image-size/enrich").json()
    by_base = {i["base"]: i for i in r["session"]["items"]}
    assert by_base["chut-thai-ayutthaya-ruins.png"]["status"] == "pending"
    assert by_base["chut-thai-ayutthaya-ruins.png"]["embeds"][0]["id"] == POST_ID
    assert by_base["pom-phet-diamond-fortress-ayutthaya.png"]["status"] == "divi-hidden"
    sess = client.get("/api/thailandnow/seo/reports/image-size/session").json()
    ayudhya = next(p for p in sess["pages"] if p["post_id"] == POST_ID)
    assert len(ayudhya["pending"]) == 1


def test_plan_and_apply_roundtrip(client, _fakes, monkeypatch):
    _mock_http(monkeypatch)
    client.post("/api/thailandnow/seo/reports/import", json={"csv_text": CSV, "filename": CSV_NAME})
    client.post("/api/thailandnow/seo/reports/image-size/enrich")

    plan = client.post("/api/thailandnow/seo/reports/image-size/plan",
                       json={"post_id": POST_ID}).json()
    assert plan["plan"][0]["refs"] == 1
    assert plan["plan"][0]["new_kb"] > 0

    applied = client.post("/api/thailandnow/seo/reports/image-size/apply",
                          json={"post_id": POST_ID}).json()
    assert applied["status"] == "applied"
    assert applied["leftovers"] == []
    assert applied["applied"][0]["new_url"] == NEW_URL
    # content rewritten, old ref gone
    assert NEW_URL in _fakes["raw"] and "chut-thai-ayutthaya-ruins-682x1024.png" not in _fakes["raw"]
    # backlog issue auto-closed
    bl = client.get("/api/thailandnow/seo/reports/backlog").json()
    issue = next(x for x in bl["issues"] if x["url"] == IMAGE_URL)
    assert issue["status"] == "fixed"
    # session shows applied
    sess = client.get("/api/thailandnow/seo/reports/image-size/session").json()
    ayudhya = next(p for p in sess["pages"] if p["post_id"] == POST_ID)
    assert ayudhya["done"] == 1


def test_revert_restores_snapshot(client, _fakes, monkeypatch):
    _mock_http(monkeypatch)
    client.post("/api/thailandnow/seo/reports/import", json={"csv_text": CSV, "filename": CSV_NAME})
    client.post("/api/thailandnow/seo/reports/image-size/enrich")
    client.post("/api/thailandnow/seo/reports/image-size/plan", json={"post_id": POST_ID})
    client.post("/api/thailandnow/seo/reports/image-size/apply", json={"post_id": POST_ID})
    r = client.post("/api/thailandnow/seo/reports/image-size/revert", json={"post_id": POST_ID}).json()
    assert r["reverted"] is True
    assert IMAGE_URL.rsplit(".", 1)[0] + "-682x1024.png" in _fakes["raw"]  # original ref back


def test_paced_pagination_resumes_from_checkpoint(monkeypatch, tmp_path):
    """Ping-pong pagination: a WAF swing on page 2 costs one page, not the match —
    the checkpoint remembers where we died and the retry re-serves from there."""
    import asyncio
    monkeypatch.setattr(seo_reports, "CHECKPOINT_FILE", tmp_path / "cp.json")
    calls = {"n": 0}

    class FakeTN:
        async def _wp(self, method, path, params=None, json_body=None):
            calls["n"] += 1
            page = params["page"]
            if page == 2 and calls["n"] == 2:
                raise RuntimeError("curl: (56) Connection closed abruptly")
            if page == 1:
                return [{"id": 1}]
            if page == 2:
                return [{"id": 2}]
            return []  # done

    out = asyncio.run(seo_reports.wp_list_all_paced(
        FakeTN(), "/media", "id", "media", page_sleep=0))
    assert [x["id"] for x in out] == [1, 2]
    assert calls["n"] == 4  # p1, p2-fail, p2-retry, p3(end)
    cp = seo_reports._cp_load()
    assert cp.get("media") is None  # completed sweep clears the checkpoint
