"""NotebookLM "RESEARCH" module: argv construction, gating, catalog, job store.

All HTTP + subprocess mocked — passes with no CLI / no Google auth. No
pytest-asyncio: async cases wrap the call in ``asyncio.run`` (same shape as
``test_comfyui.py``). Job flows are exercised directly (``_flow_*``) rather
than via the spawn endpoint, so the multi-step argv is observable without
racing the background task.
"""

import asyncio
import json
import signal
import time

import pytest
from fastapi import HTTPException

from app import notebooklm


# ---------------------------------------------------------------- fakes


class _Stream:
    def __init__(self, lines):
        self._l = list(lines)

    async def readline(self):
        return self._l.pop(0) if self._l else b""


class _SyncProc:
    """For _run_cli (sync endpoints): communicate() -> (out, err) + returncode."""

    def __init__(self, out=b"{}", err=b"", rc=0):
        self._out, self._err, self.returncode = out, err, rc

    async def communicate(self):
        return self._out, self._err

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


class _StepProc:
    """For _run_step (job flows): readable stdout/stderr + wait() + signal log."""

    def __init__(self, out_lines=(), err_lines=(), rc=0):
        self.stdout = _Stream(out_lines)
        self.stderr = _Stream(err_lines)
        self._rc = rc
        self.signals = []

    async def wait(self):
        return self._rc

    def send_signal(self, s):
        self.signals.append(s)

    def kill(self):
        pass


def _patch_exec(monkeypatch, proc):
    """Every call returns the same proc; returns the recorded argv list."""
    calls = []

    async def fake(*argv, **kw):
        calls.append(list(argv))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


def _patch_exec_seq(monkeypatch, procs):
    """Calls return procs in order (one per job step); returns recorded argv."""
    calls = []
    it = iter(procs)

    async def fake(*argv, **kw):
        calls.append(list(argv))
        return next(it)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


@pytest.fixture
def nblm_opts(monkeypatch, tmp_path):
    out = tmp_path / "nblm-out"
    out.mkdir()
    monkeypatch.setattr(notebooklm, "_OPTS",
                        {"output_dir": str(out), "browse_root": str(tmp_path)})
    monkeypatch.setattr(notebooklm, "_availability", lambda: (True, None))
    notebooklm._NB_CACHE = None
    notebooklm._AUTH_CACHE = None
    notebooklm._JOBS.clear()
    return {"out": out, "browse": tmp_path}


# ---------------------------------------------------------------- argv: sync endpoints


def test_state_auth_argv_and_cached(nblm_opts, monkeypatch):
    proc = _SyncProc(out=b'{"authenticated":true,"account":"naz@x.com"}')
    calls = _patch_exec(monkeypatch, proc)
    out = asyncio.run(notebooklm.state())
    assert calls[0] == [notebooklm.CLI, "auth", "check", "--json"]
    assert out == {"available": True, "reason": None, "authed": True, "account": "naz@x.com"}
    calls.clear()  # cached 60s → second call must not hit the CLI
    assert asyncio.run(notebooklm.state())["authed"] is True
    assert calls == []


def test_state_auth_status_ok_schema(nblm_opts, monkeypatch):
    # CLI 0.3.3 reports {"status": "ok", ...} — no top-level authenticated/account key.
    _patch_exec(monkeypatch, _SyncProc(out=b'{"status":"ok","checks":{"sid_cookie":true}}'))
    out = asyncio.run(notebooklm.state())
    assert out["authed"] is True and out["account"] is None


def test_state_auth_error_means_not_authed(nblm_opts, monkeypatch):
    _patch_exec(monkeypatch, _SyncProc(err=b"no token", rc=1))
    out = asyncio.run(notebooklm.state())
    assert out["authed"] is False and out["account"] is None


def test_state_unavailable_short_circuits(monkeypatch):
    monkeypatch.setattr(notebooklm, "_availability", lambda: (False, "notebooklm CLI not found"))

    async def boom(*a, **k):
        raise AssertionError("must not exec when unavailable")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    out = asyncio.run(notebooklm.state())
    assert out == {"available": False, "reason": "notebooklm CLI not found",
                   "authed": False, "account": None}


def test_state_availability_recomputed_per_request(monkeypatch):
    """The boot-time answer must NOT stick: unavailable on one request can flip
    to available on the next without a process restart."""
    answers = iter([(False, "notebooklm CLI not found"), (True, None)])
    monkeypatch.setattr(notebooklm, "_availability", lambda: next(answers))
    notebooklm._AUTH_CACHE = None
    _patch_exec(monkeypatch, _SyncProc(out=b'{"status":"ok"}'))
    assert asyncio.run(notebooklm.state())["available"] is False
    out = asyncio.run(notebooklm.state())
    assert out["available"] is True and out["authed"] is True


# ---------------------------------------------------------------- CLI resolution + probe


def test_resolve_cli_prefers_which(monkeypatch):
    monkeypatch.setattr(notebooklm.shutil, "which", lambda n: "/usr/bin/notebooklm")
    assert notebooklm._resolve_cli() == "/usr/bin/notebooklm"


def test_resolve_cli_falls_back_to_well_known_dir(monkeypatch, tmp_path):
    # systemd boot PATH misses ~/.local/bin — which() fails, fallback must hit
    monkeypatch.setattr(notebooklm.shutil, "which", lambda n: None)
    cli = tmp_path / "notebooklm"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setattr(notebooklm, "_FALLBACK_CLI_DIRS", [tmp_path])
    assert notebooklm._resolve_cli() == str(cli)


def test_resolve_cli_ignores_non_executable_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(notebooklm.shutil, "which", lambda n: None)
    plain = tmp_path / "notebooklm"
    plain.write_text("data")
    plain.chmod(0o644)  # exists but not executable → not the CLI
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(notebooklm, "_FALLBACK_CLI_DIRS", [empty, tmp_path])
    assert notebooklm._resolve_cli() is None


def test_availability_refreshes_cli_argv0(monkeypatch, tmp_path):
    monkeypatch.setattr(notebooklm, "CLI", notebooklm.CLI)  # auto-restore after test
    monkeypatch.setattr(notebooklm, "_OPTS", {"output_dir": str(tmp_path)})
    monkeypatch.setattr(notebooklm.shutil, "which", lambda n: None)
    cli = tmp_path / "notebooklm"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setattr(notebooklm, "_FALLBACK_CLI_DIRS", [tmp_path])
    assert notebooklm._availability() == (True, None)
    assert notebooklm.CLI == str(cli)  # later argv[0]s use the resolved path


def test_availability_reason_cli_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(notebooklm.shutil, "which", lambda n: None)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(notebooklm, "_FALLBACK_CLI_DIRS", [empty])
    monkeypatch.setattr(notebooklm, "_OPTS", {"output_dir": str(tmp_path)})
    ok, reason = notebooklm._availability()
    assert ok is False and "not found" in reason and ".local/bin" in reason


def test_availability_reason_not_registered(monkeypatch):
    monkeypatch.setattr(notebooklm.shutil, "which", lambda n: "/usr/bin/notebooklm")
    monkeypatch.setattr(notebooklm, "_OPTS", {})
    ok, reason = notebooklm._availability()
    assert ok is False and "not registered" in reason


def test_probe_bypasses_auth_cache(nblm_opts, monkeypatch):
    # poison the auth cache with "not authed" far in the future; probe must
    # drop it and hit the CLI fresh
    notebooklm._AUTH_CACHE = (time.monotonic() + 9999, (False, None))
    calls = _patch_exec(monkeypatch, _SyncProc(out=b'{"status":"ok","account":"naz@x.com"}'))
    out = asyncio.run(notebooklm.probe())
    assert calls[0] == [notebooklm.CLI, "auth", "check", "--json"]
    assert out == {"available": True, "reason": None, "authed": True, "account": "naz@x.com"}


def test_probe_unavailable_reports_reason(monkeypatch):
    monkeypatch.setattr(notebooklm, "_availability",
                        lambda: (False, "notebooklm CLI not found — checked PATH and ~/.local/bin"))

    async def boom(*a, **k):
        raise AssertionError("must not exec when unavailable")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    out = asyncio.run(notebooklm.probe())
    assert out["available"] is False and "not found" in out["reason"]
    assert out["authed"] is False and out["account"] is None


def test_probe_auth_failure_plain_words(nblm_opts, monkeypatch):
    _patch_exec(monkeypatch, _SyncProc(err=b"no token", rc=1))
    out = asyncio.run(notebooklm.probe())
    assert out["available"] is True and out["authed"] is False
    assert "notebooklm login" in out["reason"]


def test_notebooks_argv_and_alpha_sort(nblm_opts, monkeypatch):
    raw = {"notebooks": [{"id": "b", "title": "Zebra"},
                         {"id": "a", "title": "apple"},
                         {"id": "c", "title": "Banana"}]}
    calls = _patch_exec(monkeypatch, _SyncProc(out=json.dumps(raw).encode()))
    out = asyncio.run(notebooklm.notebooks())
    assert calls[0] == [notebooklm.CLI, "list", "--json"]
    assert [n["title"] for n in out["notebooks"]] == ["apple", "Banana", "Zebra"]


def test_notebooks_502_on_cli_error(nblm_opts, monkeypatch):
    _patch_exec(monkeypatch, _SyncProc(err=b"auth expired", rc=1))
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.notebooks())
    assert ex.value.status_code == 502 and "auth expired" in ex.value.detail


def test_create_notebook_argv_and_cache_invalidate(nblm_opts, monkeypatch):
    calls = _patch_exec(monkeypatch, _SyncProc(out=b'{"id":"n1","title":"New"}'))
    out = asyncio.run(notebooklm.create_notebook(notebooklm.CreateBody(title="New")))
    assert calls[0] == [notebooklm.CLI, "create", "New", "--json"]
    assert out == {"id": "n1", "title": "New"}
    assert notebooklm._NB_CACHE is None  # create invalidates the list cache


def test_delete_mismatch_returns_400(nblm_opts, monkeypatch):
    raw = {"notebooks": [{"id": "n1", "title": "Real Title"}]}
    calls = _patch_exec(monkeypatch, _SyncProc(out=json.dumps(raw).encode()))
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.delete_notebook("n1", notebooklm.DeleteBody(confirm_title="wrong")))
    assert ex.value.status_code == 400
    assert all("delete" not in c for c in calls)  # only the list call ran


def test_delete_match_runs_delete_argv(nblm_opts, monkeypatch):
    raw = {"notebooks": [{"id": "n1", "title": "Real Title"}]}
    calls = _patch_exec(monkeypatch, _SyncProc(out=json.dumps(raw).encode()))
    out = asyncio.run(notebooklm.delete_notebook(
        "n1", notebooklm.DeleteBody(confirm_title="Real Title")))
    assert out == {"deleted": "n1"}
    assert [notebooklm.CLI, "delete", "-n", "n1", "-y"] in calls
    assert notebooklm._NB_CACHE is None


def test_delete_404_unknown_notebook(nblm_opts, monkeypatch):
    raw = {"notebooks": [{"id": "other", "title": "X"}]}
    _patch_exec(monkeypatch, _SyncProc(out=json.dumps(raw).encode()))
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.delete_notebook("ghost", notebooklm.DeleteBody(confirm_title="X")))
    assert ex.value.status_code == 404


def test_sources_list_argv(nblm_opts, monkeypatch):
    calls = _patch_exec(monkeypatch, _SyncProc(out=b'{"sources":[{"source_id":"s1"}]}'))
    out = asyncio.run(notebooklm.sources("n1"))
    assert calls[0] == [notebooklm.CLI, "source", "list", "--json", "--notebook", "n1"]
    assert out == {"sources": [{"source_id": "s1"}]}


def test_ask_argv_without_and_with_conversation(nblm_opts, monkeypatch):
    calls = _patch_exec(monkeypatch, _SyncProc(out=b'{"answer":"x"}'))
    asyncio.run(notebooklm.ask("n1", notebooklm.AskBody(question="hi")))
    assert calls[0] == [notebooklm.CLI, "ask", "hi", "--json", "--notebook", "n1"]

    calls = _patch_exec(monkeypatch, _SyncProc(out=b'{"answer":"y"}'))
    asyncio.run(notebooklm.ask("n1", notebooklm.AskBody(question="again", conversation_id="c1")))
    assert calls[0] == [notebooklm.CLI, "ask", "again", "--json", "--notebook", "n1", "-c", "c1"]


def test_ask_502_on_cli_error(nblm_opts, monkeypatch):
    _patch_exec(monkeypatch, _SyncProc(err=b"boom", rc=1))
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.ask("n1", notebooklm.AskBody(question="hi")))
    assert ex.value.status_code == 502


def test_artifacts_list_argv(nblm_opts, monkeypatch):
    calls = _patch_exec(monkeypatch, _SyncProc(out=b'{"artifacts":[{"id":"a1"}]}'))
    out = asyncio.run(notebooklm.artifacts("n1"))
    assert calls[0] == [notebooklm.CLI, "artifact", "list", "--json", "--notebook", "n1"]
    assert out == {"artifacts": [{"id": "a1"}]}


# ---------------------------------------------------------------- argv: job flows


def test_source_flow_url_argv(nblm_opts, monkeypatch):
    # real CLI 0.3.3 wraps the source: {"source": {"id": ...}}
    calls = _patch_exec_seq(monkeypatch, [
        _StepProc(out_lines=[b'{"source":{"id":"s1","title":"t","status":"ready"}}']),
        _StepProc(out_lines=[b"done"]),
    ])
    job = notebooklm.Job(id="x", kind="source", label="s")
    asyncio.run(notebooklm._flow_source(job, "n1", "https://x.com/a"))
    assert calls[0] == [notebooklm.CLI, "source", "add", "https://x.com/a", "--json", "--notebook", "n1"]
    assert calls[1] == [notebooklm.CLI, "source", "wait", "s1", "-n", "n1", "--timeout", "120"]
    assert job.progress == 50


def test_source_flow_path_uses_resolved_target(nblm_opts, monkeypatch):
    f = nblm_opts["browse"] / "doc.pdf"
    f.write_bytes(b"hi")
    calls = _patch_exec_seq(monkeypatch, [
        _StepProc(out_lines=[b'{"source_id":"s9"}']), _StepProc()])
    job = notebooklm.Job(id="x", kind="source", label="s")
    asyncio.run(notebooklm._flow_source(job, "n1", str(f)))
    assert calls[0] == [notebooklm.CLI, "source", "add", str(f), "--json", "--notebook", "n1"]


def test_source_add_path_outside_browse_400(nblm_opts, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("must not exec")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.add_source("n1", notebooklm.SourceBody(path="/etc/passwd")))
    assert ex.value.status_code == 400


def test_source_add_path_inside_spawns(nblm_opts, monkeypatch):
    f = nblm_opts["browse"] / "doc.pdf"
    f.write_bytes(b"hi")
    rec = {}
    monkeypatch.setattr(notebooklm, "_spawn",
                        lambda kind, label, mf: rec.update(kind=kind, label=label) or {"id": "j1"})
    out = asyncio.run(notebooklm.add_source("n1", notebooklm.SourceBody(path=str(f))))
    assert out == {"id": "j1"} and rec["kind"] == "source" and "doc.pdf" in rec["label"]


def test_research_flow_argv(nblm_opts, monkeypatch):
    calls = _patch_exec_seq(monkeypatch, [_StepProc(out_lines=[b"queued"]), _StepProc(out_lines=[b"ok"])])
    job = notebooklm.Job(id="r", kind="research", label="r")
    asyncio.run(notebooklm._flow_research(job, "n1", "quantum computing", "deep"))
    assert calls[0] == [notebooklm.CLI, "source", "add-research", "quantum computing",
                         "--mode", "deep", "--no-wait", "--notebook", "n1"]
    assert calls[1] == [notebooklm.CLI, "research", "wait", "-n", "n1",
                         "--import-all", "--timeout", "300"]
    assert job.progress == 20


def test_generate_flow_argv_and_dest(nblm_opts, monkeypatch):
    calls = _patch_exec_seq(monkeypatch, [
        _StepProc(out_lines=[b'{"task_id":"t1","status":"queued"}']),
        _StepProc(out_lines=[b"ready"]),
        _StepProc(out_lines=[b"saved"]),
    ])
    job = notebooklm.Job(id="g", kind="generate", label="audio")
    asyncio.run(notebooklm._flow_generate(job, "n1", "audio", ["--format", "deep-dive"],
                                          None, "My NB", "mp3"))
    assert calls[0] == [notebooklm.CLI, "generate", "audio", "--json", "--notebook", "n1",
                         "--format", "deep-dive"]
    assert calls[1] == [notebooklm.CLI, "artifact", "wait", "t1", "-n", "n1", "--timeout", "3600"]
    dest = str(nblm_opts["out"] / "my-nb" / "audio-t1.mp3")
    assert calls[2] == [notebooklm.CLI, "download", "audio", dest, "-a", "t1", "-n", "n1"]
    assert job.output_paths == [dest]
    assert job.progress == 90 and (nblm_opts["out"] / "my-nb").is_dir()


def test_generate_flow_with_instructions_and_aid_truncation(nblm_opts, monkeypatch):
    calls = _patch_exec_seq(monkeypatch, [
        _StepProc(out_lines=[b'{"task_id":"abc123def456"}']), _StepProc(), _StepProc()])
    job = notebooklm.Job(id="g", kind="generate", label="data-table")
    asyncio.run(notebooklm._flow_generate(job, "n1", "data-table", [], "build a table of X", "NB", "csv"))
    # instructions sit between type and --json; aid truncated to 8 chars in filename
    assert calls[0] == [notebooklm.CLI, "generate", "data-table", "build a table of X",
                         "--json", "--notebook", "n1"]
    assert calls[2][3].endswith("data-table-abc123de.csv")


def test_download_flow_argv(nblm_opts, monkeypatch):
    calls = _patch_exec_seq(monkeypatch, [_StepProc(), _StepProc()])
    job = notebooklm.Job(id="d", kind="download", label="d")
    asyncio.run(notebooklm._flow_download(job, "n1", "report", "art99", "My NB", "md"))
    assert calls[0] == [notebooklm.CLI, "artifact", "wait", "art99", "-n", "n1", "--timeout", "600"]
    dest = str(nblm_opts["out"] / "my-nb" / "report-art99.md")
    assert calls[1] == [notebooklm.CLI, "download", "report", dest, "-a", "art99", "-n", "n1"]
    assert job.output_paths == [dest]


# ---------------------------------------------------------------- gating


def test_generate_409_when_generate_running(nblm_opts):
    notebooklm._JOBS["old"] = notebooklm.Job(id="old", kind="generate", label="g", status="running")
    # a source job running must NOT trigger the 409
    notebooklm._JOBS["s"] = notebooklm.Job(id="s", kind="source", label="s", status="running")
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.generate("n1", notebooklm.GenerateBody(type="audio", options={"--format": "deep-dive"})))
    assert ex.value.status_code == 409


def test_generate_not_blocked_by_source_job(nblm_opts, monkeypatch):
    notebooklm._JOBS["s"] = notebooklm.Job(id="s", kind="source", label="s", status="running")

    async def fake_nb(refresh=False):
        return [{"id": "n1", "title": "My NB"}]
    monkeypatch.setattr(notebooklm, "_cached_notebooks", fake_nb)
    monkeypatch.setattr(notebooklm, "_spawn", lambda *a, **k: {"id": "j1"})
    out = asyncio.run(notebooklm.generate("n1", notebooklm.GenerateBody(type="audio", options={"--format": "deep-dive"})))
    assert out == {"id": "j1"}


def test_generate_unknown_type_400(nblm_opts):
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.generate("n1", notebooklm.GenerateBody(type="nope")))
    assert ex.value.status_code == 400


def test_generate_bad_option_value_400(nblm_opts):
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.generate("n1", notebooklm.GenerateBody(
            type="audio", options={"--format": "nope"})))
    assert ex.value.status_code == 400


def test_generate_unknown_option_flag_400(nblm_opts):
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.generate("n1", notebooklm.GenerateBody(
            type="audio", options={"--bogus": "x"})))
    assert ex.value.status_code == 400


def test_generate_data_table_needs_instructions_400(nblm_opts):
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.generate("n1", notebooklm.GenerateBody(type="data-table")))
    assert ex.value.status_code == 400


def test_download_artifact_unknown_type_400(nblm_opts):
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.download_artifact(
            notebooklm.DownloadBody(notebook_id="n1", artifact_id="a1", type="nope")))
    assert ex.value.status_code == 400


# ---------------------------------------------------------------- catalog + job store


def test_catalog_shape():
    types = notebooklm.catalog()["types"]
    assert {t["id"] for t in types} == {"audio", "video", "slide-deck", "infographic",
                                        "report", "mind-map", "data-table", "quiz", "flashcards"}
    for t in types:
        assert {"id", "label", "ext", "groups", "needs_instructions"} <= set(t)
        assert isinstance(t["groups"], list)  # every type has a groups list (maybe empty)
        for g in t["groups"]:
            assert set(g) == {"flag", "label", "values"} and g["values"]
    dt = next(t for t in types if t["id"] == "data-table")
    assert dt["needs_instructions"] is True and dt["ext"] == "csv" and dt["groups"] == []
    mm = next(t for t in types if t["id"] == "mind-map")
    assert mm["needs_instructions"] is False and mm["ext"] == "json" and mm["groups"] == []
    audio = next(t for t in types if t["id"] == "audio")
    assert audio["ext"] == "mp3" and {"--format", "--length"} <= {g["flag"] for g in audio["groups"]}


def test_job_to_dict_shape_excludes_internals():
    j = notebooklm.Job(id="x", kind="generate", label="g")
    j.logs.append("hi")
    j.output_paths = ["/o/a.mp3"]
    d = j.to_dict()
    assert set(d) == {"id", "kind", "label", "status", "progress",
                      "output_paths", "error", "logs"}
    assert "proc" not in d and "cancel" not in d
    assert d["output_paths"] == ["/o/a.mp3"] and d["logs"] == ["hi"]
    assert d["status"] == "queued" and d["progress"] == 0


def test_logs_deque_maxlen_200():
    j = notebooklm.Job(id="x", kind="source", label="s")
    for i in range(250):
        j.logs.append(f"line {i}")
    assert len(j.logs) == 200 and list(j.logs)[-1] == "line 249"


def test_run_job_done_error_cancelled():
    async def ok(job):
        job.progress = 90
    j = notebooklm.Job(id="a", kind="generate", label="g")
    asyncio.run(notebooklm._run_job(j, ok(j)))
    assert j.status == "done" and j.progress == 100

    async def boom(job):
        raise RuntimeError("step failed")
    j = notebooklm.Job(id="b", kind="source", label="s")
    asyncio.run(notebooklm._run_job(j, boom(j)))
    assert j.status == "error" and j.error == "step failed"

    async def can(job):
        raise notebooklm._Cancelled()
    j = notebooklm.Job(id="c", kind="source", label="s", status="running")
    asyncio.run(notebooklm._run_job(j, can(j)))
    assert j.status == "cancelled"


# ---------------------------------------------------------------- outputs + path confinement


def test_outputs_file_rejects_escape(nblm_opts):
    with pytest.raises(HTTPException) as ex:
        notebooklm.outputs_file(path="/etc/passwd")
    assert ex.value.status_code == 403


def test_outputs_list_and_notebook_filter(nblm_opts):
    base = nblm_opts["out"]
    (base / "my-nb").mkdir(parents=True)
    (base / "other-nb").mkdir(parents=True)
    (base / "my-nb" / "audio-xyz.mp3").write_bytes(b"")
    (base / "other-nb" / "report-a.md").write_bytes(b"")
    all_files = {f["name"] for f in notebooklm.outputs()["files"]}
    assert all_files == {"audio-xyz.mp3", "report-a.md"}
    assert {f["name"] for f in notebooklm.outputs(notebook="my-nb")["files"]} == {"audio-xyz.mp3"}
    assert notebooklm.outputs()["files"][0]["ext"] in ("mp3", "md")


# ---------------------------------------------------------------- polish + cancel


def test_polish_uses_zai_message(monkeypatch):
    seen = {}

    async def fake_zai(prompt, max_tokens=400):
        seen["prompt"] = prompt
        return "polished text"
    monkeypatch.setattr(notebooklm, "zai_message", fake_zai)
    out = asyncio.run(notebooklm.polish(notebooklm.PolishBody(draft="make vid", purpose="generate")))
    assert out == {"polished": "polished text"}
    assert "generation instruction" in seen["prompt"] and "make vid" in seen["prompt"]


def test_polish_bad_purpose_400():
    with pytest.raises(HTTPException) as ex:
        asyncio.run(notebooklm.polish(notebooklm.PolishBody(draft="x", purpose="other")))
    assert ex.value.status_code == 400


def test_cancel_job_sends_sigterm(nblm_opts):
    job = notebooklm.Job(id="r", kind="research", label="r", status="running")
    proc = _StepProc()
    job.proc = proc
    notebooklm._JOBS["r"] = job
    asyncio.run(notebooklm.cancel_job("r"))
    assert job.cancel is True and proc.signals == [signal.SIGTERM]
