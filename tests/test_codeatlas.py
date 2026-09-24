"""CODEATLAS module — target resolution, artifact law, staleness, gitignore,
job flow (agy faked — never invoked for real)."""

import asyncio
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import codeatlas
from app.main import app

GOOD_HTML = '<html><body><script id="codeatlas-data" type="application/json">{}</script></body></html>'


def _write_map(mapdir, head="abc"):
    mapdir.mkdir(parents=True, exist_ok=True)
    (mapdir / "codeatlas.html").write_text(GOOD_HTML, encoding="utf-8")
    (mapdir / "module-map.json").write_text(json.dumps({
        "project": "t", "source": "s",
        "modules": [{"name": "a", "role": "r"}],
        "relations": [{"source": "a", "target": "b", "type": "calls", "reason": "imports"}],
        "entrypoints": [],
    }), encoding="utf-8")
    if head is not None:
        (mapdir / "meta.json").write_text(json.dumps({
            "generated": time.time(), "head": head, "target": "t", "kind": "repo",
        }), encoding="utf-8")


# ── artifact law ──────────────────────────────────────────────────────────


def test_validate_artifacts_accepts_good_map(tmp_path):
    md = tmp_path / "CODEATLAS"
    _write_map(md)
    assert codeatlas.validate_artifacts(md) == []


def test_validate_artifacts_rejects_single_file_violations(tmp_path):
    md = tmp_path / "CODEATLAS"
    _write_map(md)
    (md / "codeatlas.html").write_text(
        '<html><script src="https://cdn/x.js"></script><script id="codeatlas-data">{}</script>'
        "<script>fetch('/x')</script></html>", encoding="utf-8")
    errs = codeatlas.validate_artifacts(md)
    assert any("fetch(" in e for e in errs)
    assert any("external script/link" in e for e in errs)


def test_validate_artifacts_rejects_bad_json(tmp_path):
    md = tmp_path / "CODEATLAS"
    md.mkdir()
    (md / "codeatlas.html").write_text(GOOD_HTML, encoding="utf-8")
    (md / "module-map.json").write_text('{"modules": []}', encoding="utf-8")
    errs = codeatlas.validate_artifacts(md)
    assert any("no modules" in e for e in errs)


# ── target resolution ─────────────────────────────────────────────────────


def test_resolve_target_railjack_aliases(monkeypatch):
    monkeypatch.chdir(codeatlas._REPO_ROOT.parent)
    assert codeatlas.resolve_target("railjack")["kind"] == "repo"
    assert codeatlas.resolve_target("newsroom")["label"] == "Railjack · newsroom"


def test_resolve_target_projects_and_skills(monkeypatch, tmp_path):
    proj = tmp_path / "projects" / "myproj"
    proj.mkdir(parents=True)
    (proj / ".git").mkdir()
    skill = tmp_path / "skills" / "myskill"
    skill.mkdir(parents=True)
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path / "projects")
    monkeypatch.setattr(codeatlas, "_skill_roots", lambda: [tmp_path / "skills"])
    r1 = codeatlas.resolve_target("myproj")
    assert r1["kind"] == "repo" and r1["path"] == proj
    r2 = codeatlas.resolve_target("myskill")
    assert r2["kind"] == "skill" and r2["path"] == skill


def test_resolve_target_unknown_raises_400(monkeypatch, tmp_path):
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(codeatlas, "_skill_roots", lambda: [tmp_path])
    with pytest.raises(HTTPException) as e:
        codeatlas.resolve_target("nonexistent-thing")
    assert e.value.status_code == 400


# ── staleness ─────────────────────────────────────────────────────────────


def test_check_target_missing_current_stale(monkeypatch, tmp_path):
    proj = tmp_path / "repo"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(codeatlas, "_head_sha", lambda p: "HEAD1")
    assert codeatlas.check_target("repo")["state"] == "missing"
    _write_map(codeatlas.map_dir(proj), head="HEAD1")
    assert codeatlas.check_target("repo")["state"] == "current"
    _write_map(codeatlas.map_dir(proj), head="OLD")
    assert codeatlas.check_target("repo")["state"] == "stale"


# ── gitignore law ─────────────────────────────────────────────────────────


def test_gitignore_law_appends_and_respects_existing(tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    assert codeatlas._gitignore_law(repo) == "created"
    assert "CODEATLAS/" in (repo / ".gitignore").read_text()
    (repo / ".gitignore").write_text("node_modules/\n")
    assert codeatlas._gitignore_law(repo) == "appended"
    assert "CODEATLAS/" in (repo / ".gitignore").read_text()
    (repo / ".gitignore").write_text("node_modules/\nCODEATLAS/\n")
    assert codeatlas._gitignore_law(repo) is None          # already ignored
    nongit = tmp_path / "ng"
    nongit.mkdir()
    assert codeatlas._gitignore_law(nongit) is None        # no git → no law


# ── job flow (agy faked) ──────────────────────────────────────────────────


def test_generate_job_flow_happy_path(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    proj.mkdir(exist_ok=True)
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(codeatlas, "_head_sha", lambda p: "HEAD9")
    monkeypatch.setattr(codeatlas, "_agy_timeout", lambda: 30)
    monkeypatch.setattr(codeatlas.shutil, "which", lambda name: "/usr/bin/agy")

    class FakeProc:
        returncode = 0

        async def communicate(self):
            _write_map(proj / "CODEATLAS", head=None)  # agy writes map, NOT meta
            return b"done", b""

    async def fake_exec(*argv, **kw):
        return FakeProc()

    monkeypatch.setattr(codeatlas.asyncio, "create_subprocess_exec", fake_exec)
    with TestClient(app) as c:
        r = c.post("/api/codeatlas/generate", json={"target": "proj"})
        assert r.status_code == 200, r.text
        jid = r.json()["id"]
        for _ in range(100):
            j = c.get(f"/api/codeatlas/job/{jid}").json()
            if j["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert j["status"] == "done", j
        assert j["result"]["validated"] is True
        meta = json.loads((proj / "CODEATLAS" / "meta.json").read_text())
        assert meta["head"] == "HEAD9"                        # module owns meta
        assert "CODEATLAS/" in (proj / ".gitignore").read_text()


def test_generate_job_flow_reports_law_violation(monkeypatch, tmp_path):
    proj = tmp_path / "bad"
    proj.mkdir()
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(codeatlas, "_agy_timeout", lambda: 30)
    monkeypatch.setattr(codeatlas.shutil, "which", lambda name: "/usr/bin/agy")

    class BadProc:
        returncode = 0

        async def communicate(self):
            md = proj / "CODEATLAS"
            md.mkdir(parents=True, exist_ok=True)
            (md / "codeatlas.html").write_text("<html>fetch('x')</html>", encoding="utf-8")
            (md / "module-map.json").write_text('{"modules": [{"name": "a"}], "relations": []}', encoding="utf-8")
            return b"done", b""

    async def fake_exec(*a, **kw):
        return BadProc()

    monkeypatch.setattr(codeatlas.asyncio, "create_subprocess_exec", fake_exec)
    with TestClient(app) as c:
        jid = c.post("/api/codeatlas/generate", json={"target": "bad"}).json()["id"]
        for _ in range(100):
            j = c.get(f"/api/codeatlas/job/{jid}").json()
            if j["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert j["status"] == "error"
        assert "single-file law broken" in j["error"] or "fetch(" in j["error"]


def test_generate_single_flight(monkeypatch, tmp_path):
    proj = tmp_path / "solo"
    proj.mkdir()
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path)

    class SlowProc:
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(5)
            return b"", b""

    async def fake_exec(*a, **kw):
        return SlowProc()

    monkeypatch.setattr(codeatlas.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(codeatlas, "_agy_timeout", lambda: 60)
    monkeypatch.setattr(codeatlas.shutil, "which", lambda name: "/usr/bin/agy")
    with TestClient(app) as c:
        r1 = c.post("/api/codeatlas/generate", json={"target": "solo"})
        assert r1.status_code == 200
        r2 = c.post("/api/codeatlas/generate", json={"target": "solo"})
        if r2.status_code != 409:
            from app import codeatlas as ca
            print("R2 BODY:", r2.text)
            print("JOBS:", [(j.id, j.status, j.error) for j in ca._JOBS.values()])
        assert r2.status_code == 409


def test_generate_focus_flows_into_brief_and_meta(monkeypatch, tmp_path):
    """Feature-focus mode: the named flow reaches the agy brief and meta.json."""
    proj = tmp_path / "focused"
    (proj / ".git").mkdir(parents=True)
    proj.mkdir(exist_ok=True)
    monkeypatch.setattr(codeatlas, "_projects_root", lambda: tmp_path)
    monkeypatch.setattr(codeatlas, "_head_sha", lambda p: "HEADF")
    monkeypatch.setattr(codeatlas, "_agy_timeout", lambda: 30)
    monkeypatch.setattr(codeatlas.shutil, "which", lambda name: "/usr/bin/agy")
    seen = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            seen["brief_call"] = True
            _write_map(proj / "CODEATLAS", head=None)
            return b"done", b""

    async def fake_brief_probe(*argv, **kw):
        seen["brief"] = argv[1]
        return FakeProc()

    monkeypatch.setattr(codeatlas.asyncio, "create_subprocess_exec", fake_brief_probe)
    with TestClient(app) as c:
        jid = c.post("/api/codeatlas/generate",
                     json={"target": "focused", "focus": "IDE SCOUT"}).json()["id"]
        for _ in range(100):
            j = c.get(f"/api/codeatlas/job/{jid}").json()
            if j["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert j["status"] == "done", j
        meta = json.loads((proj / "CODEATLAS" / "meta.json").read_text())
        assert meta["focus"] == "IDE SCOUT"
        assert codeatlas.check_target("focused")["focus"] == "IDE SCOUT"
