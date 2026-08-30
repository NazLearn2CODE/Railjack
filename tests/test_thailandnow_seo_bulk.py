"""SEO BULK LINK (bulk un-orphan): route + LINK ALL job with faked WP.

``_wp``/``_wp_resolve_rest_base`` are monkeypatched — nothing touches the real
site. The LINK ALL job rides the real TnJob machinery (``create_task`` on the
TestClient portal loop), polled to done inside the client context.
"""

import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import thailandnow
from app.main import app

ORPHAN = "Khon Kaen Street Food Guide"
ORPHAN_LINK = "https://www.thailandnow.in.th/khon-kaen-street-food/"
HOST_WITH = "<p>Visit Khon kaen for food.</p>"  # lowercase k → casing-preserved wrap
HOST_WITHOUT = "<p>Nothing relevant here.</p>"


class _FakeWP:
    """Mirrors ``_wp(method, path, params, json_body)``; records content PUTs."""

    def __init__(self, contents: dict[int, str], fail_ids: tuple[int, ...] = ()):
        self.contents = contents
        self.fail_ids = fail_ids
        self.puts: list[tuple[int, str]] = []

    async def __call__(self, method, path, params=None, json_body=None):
        parts = path.strip("/").split("/")
        pid = int(parts[1])
        if pid in self.fail_ids:
            raise HTTPException(502, f"WP {method} {path}: 500 boom")
        if method == "GET":
            return {"id": pid, "content": {"raw": self.contents.get(pid, "")}}
        self.puts.append((pid, json_body["content"]))
        return {"id": pid}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_wp(monkeypatch):
    async def resolve(post_id: int) -> str:
        return "posts"

    def _install(contents: dict[int, str], fail_ids: tuple[int, ...] = ()) -> _FakeWP:
        wp = _FakeWP(contents, fail_ids)
        monkeypatch.setattr(thailandnow, "_wp", wp)
        monkeypatch.setattr(thailandnow, "_wp_resolve_rest_base", resolve)
        return wp

    return _install


def _hosts(*ids: int) -> list[dict]:
    return [{"id": i, "title": f"Host {i}", "link": f"https://www.thailandnow.in.th/h{i}/"} for i in ids]


def test_bulk_link_dry_run_never_writes(client, fake_wp):
    wp = fake_wp({11: HOST_WITH, 12: HOST_WITHOUT})
    r = client.post("/api/thailandnow/seo/bulk-link", json={
        "orphan_title": ORPHAN, "orphan_link": ORPHAN_LINK, "dry_run": True,
        "hosts": _hosts(11, 12),
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["linked"], d["pending"], d["noop"], d["failed"]) == (0, 1, 1, 0), d
    picked = next(x for x in d["results"] if x["host_id"] == 11)
    assert picked["phrase"] == "Khon Kaen" and picked["matches"] is None and picked["snippet"]
    noop = next(x for x in d["results"] if x["host_id"] == 12)
    assert noop["matches"] == 0 and "phrase" not in noop
    assert wp.puts == [], "dry run must not write"


def test_bulk_link_apply_wraps_and_records(client, fake_wp):
    wp = fake_wp({11: HOST_WITH, 12: HOST_WITHOUT})
    r = client.post("/api/thailandnow/seo/bulk-link", json={
        "orphan_title": ORPHAN, "orphan_link": ORPHAN_LINK, "dry_run": False,
        "hosts": _hosts(11, 12),
    })
    d = r.json()
    assert (d["linked"], d["noop"], d["failed"]) == (1, 1, 0), d
    assert len(wp.puts) == 1 and wp.puts[0][0] == 11
    assert f'<a href="{ORPHAN_LINK}">Khon kaen</a>' in wp.puts[0][1], wp.puts  # casing kept


def test_bulk_link_host_error_continues(client, fake_wp):
    wp = fake_wp({12: HOST_WITH}, fail_ids=(11,))
    r = client.post("/api/thailandnow/seo/bulk-link", json={
        "orphan_title": ORPHAN, "orphan_link": ORPHAN_LINK, "dry_run": False,
        "hosts": _hosts(11, 12),
    })
    d = r.json()
    assert (d["failed"], d["linked"]) == (1, 1), d
    bad = next(x for x in d["results"] if x["host_id"] == 11)
    assert not bad["ok"] and "boom" in bad["error"]
    assert len(wp.puts) == 1 and wp.puts[0][0] == 12  # healthy host still written


def test_bulk_link_drops_self_dupe_and_idless(client, fake_wp):
    fake_wp({11: HOST_WITH})
    r = client.post("/api/thailandnow/seo/bulk-link", json={
        "orphan_title": ORPHAN, "orphan_link": ORPHAN_LINK, "dry_run": True,
        "hosts": [
            {"id": 11, "title": "A", "link": "https://www.thailandnow.in.th/a/"},
            {"id": 11, "title": "A dupe", "link": "https://www.thailandnow.in.th/a/"},
            {"id": 12, "title": "self", "link": ORPHAN_LINK},
            {"id": None, "title": "no id", "link": "https://www.thailandnow.in.th/c/"},
        ],
    })
    d = r.json()
    assert [x["host_id"] for x in d["results"]] == [11], d
    assert d["total"] == 1


def test_bulk_link_all_job_runs_and_reports(client, fake_wp):
    wp = fake_wp({11: HOST_WITH, 21: "<p>Read more about Orphan 2 here.</p>", 22: HOST_WITHOUT})
    orphans = [
        {"title": ORPHAN, "link": ORPHAN_LINK, "suggested": _hosts(11)},
        {"title": "Orphan no hosts", "link": "https://www.thailandnow.in.th/noh/", "suggested": []},
        {"title": "Orphan 2", "link": "https://www.thailandnow.in.th/o2/", "suggested": _hosts(21, 22, 11)},
    ]
    with client:
        r = client.post("/api/thailandnow/seo/bulk-link-all", json={"orphans": orphans, "cap": 2})
        assert r.status_code == 200, r.text
        jid = r.json()["id"]
        job = thailandnow._TN_JOBS.get(jid)
        for _ in range(100):
            job = thailandnow._TN_JOBS.get(jid)
            if job and job.status in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        assert job.status == "done", (job.status, job.error)
        rep = client.get(f"/api/thailandnow/seo/bulk-link/report/{jid}")
    assert rep.status_code == 200, rep.text
    res = rep.json()
    # cap=2 keeps the two LINKABLE orphans; the no-hosts one is skipped
    assert res["processed"] == 2 and res["orphans_linked"] == 2 and res["skipped"] == 1, res
    assert set(res["linked_orphans"]) == {ORPHAN_LINK, "https://www.thailandnow.in.th/o2/"}
    # orphan 1 linked host 11; orphan 2 linked host 21, while 22 and the
    # already-linked 11 (phrase now inside an <a>) correctly noop
    assert sorted(pid for pid, _ in wp.puts) == [11, 21], wp.puts
    o2 = next(x for x in res["results"] if x["orphan_link"].endswith("/o2/"))
    assert (o2["linked"], o2["noop"]) == (1, 2), o2
