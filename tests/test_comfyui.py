"""ComfyUI module: fit math, workflow build, job-store, path/gating.

All HTTP + subprocess mocked — passes with ComfyUI down. No pytest-asyncio:
async cases wrap the call in ``asyncio.run`` (same shape as ``test_jobs.py``).
"""

import asyncio

import pytest
from fastapi import HTTPException

from app import comfyui
from app.comfyui import Job


# ---------------------------------------------------------------- fit math

def test_fit_ok_warn_no_boundaries():
    assert comfyui._fit(8, 24) == "ok"
    assert comfyui._fit(24, 24) == "ok"        # exactly at the line
    assert comfyui._fit(25, 24) == "warn"      # offload territory (<= 28.8)
    assert comfyui._fit(28.8, 24) == "warn"    # 1.2× boundary inclusive
    assert comfyui._fit(29, 24) == "no"
    assert comfyui._fit(13, 0) == "no"         # no vram → nothing fits


# ---------------------------------------------------------------- workflow build

def test_generic_t2i_uses_checkpoint_clip_and_vae():
    g = comfyui._generic_t2i("sd_xl_base_1.0.safetensors", "a cat", "ugly", 1024, 1024, 7, 1)
    assert g["4"]["class_type"] == "CheckpointLoaderSimple"
    assert g["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    # CLIP + VAE come from the checkpoint (slots 1 and 2), not separate loaders:
    assert g["5"]["inputs"]["clip"] == ["4", 1]
    assert g["8"]["inputs"]["vae"] == ["4", 2]
    # proven krea2 sampler set, reused per "same graph with ckpt_name swapped":
    ks = g["3"]["inputs"]
    assert (ks["steps"], ks["cfg"], ks["sampler_name"], ks["scheduler"]) == (4, 1.5, "euler", "simple")


def test_flow_t2i_uses_separate_clip_and_vae_loaders():
    f = comfyui._flow_t2i("krea2_turbo_fp8_scaled.safetensors",
                          "qwen3vl_4b_fp8_scaled.safetensors", "qwen_image_vae.safetensors",
                          "a cat", "ugly", 1024, 1024, 7, 1)
    types = {n["class_type"] for n in f.values()}
    assert {"CLIPLoader", "VAELoader", "CheckpointLoaderSimple"} <= types
    # CLIPTextEncode is wired to the CLIPLoader node, not the checkpoint:
    clip_id = next(k for k, n in f.items() if n["class_type"] == "CLIPLoader")
    enc = next(n for n in f.values()
               if n["class_type"] == "CLIPTextEncode" and n["inputs"]["text"] == "a cat")
    assert enc["inputs"]["clip"][0] == clip_id
    # VAEDecode wired to the VAELoader node:
    vae_id = next(k for k, n in f.items() if n["class_type"] == "VAELoader")
    assert f["17"]["inputs"]["vae"][0] == vae_id


def test_workflow_picks_flow_variant_when_entry_has_clip_and_vae():
    catalog = [{"id": "k", "components": [
        {"folder": "checkpoints", "filename": "m.safetensors"},
        {"folder": "text_encoders", "filename": "q.safetensors"},
        {"folder": "vae", "filename": "v.safetensors"},
    ]}]
    wf = comfyui._workflow("m.safetensors", "p", None, 512, 512, 1, 1, catalog)
    assert any(n["class_type"] == "CLIPLoader" for n in wf.values())


def test_workflow_falls_back_to_generic_for_lone_checkpoint():
    catalog = [{"id": "s", "components": [
        {"folder": "checkpoints", "filename": "m.safetensors"}]}]
    wf = comfyui._workflow("m.safetensors", "p", None, 512, 512, 1, 1, catalog)
    assert not any(n["class_type"] == "CLIPLoader" for n in wf.values())
    assert any(n["class_type"] == "CheckpointLoaderSimple" for n in wf.values())


# ---------------------------------------------------------------- job-store serialization

def test_job_to_dict_shape_excludes_internals():
    j = Job(id="x", kind="generate", label="cat")
    j.logs.append("hi")
    j.output_paths = ["/o/a.png"]
    d = j.to_dict()
    assert set(d) == {"id", "kind", "label", "entry_id", "status", "progress",
                      "output_paths", "error", "logs"}
    assert "proc" not in d and "prompt_id" not in d
    assert d["entry_id"] is None  # generate jobs carry no catalog entry
    assert d["output_paths"] == ["/o/a.png"]
    assert d["logs"] == ["hi"]
    assert d["status"] == "queued" and d["progress"] == 0


def test_logs_deque_maxlen_200():
    j = Job(id="x", kind="download", label="c")
    for i in range(250):
        j.logs.append(f"line {i}")
    assert len(j.logs) == 200
    assert list(j.logs)[-1] == "line 249"


# ---------------------------------------------------------------- download runner (mocked subprocess)

class _Stream:
    def __init__(self, lines):
        self._l = list(lines)

    async def readline(self):
        return self._l.pop(0) if self._l else b""


class _FakeProc:
    def __init__(self, err_lines, rc):
        self.stdout = _Stream([])
        self.stderr = _Stream(err_lines)
        self._rc = rc

    async def wait(self):
        return self._rc

    def send_signal(self, _sig):
        pass


def _exec(err=(), rc=0):
    async def fake_exec(*_a, **_k):
        return _FakeProc(list(err), rc)
    return fake_exec


def test_run_download_done_progress_100(monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _exec(err=[b"downloading 50%\n", b"downloading 100%\n"], rc=0))
    job = Job(id="d1", kind="download", label="c/x")
    asyncio.run(comfyui._run_download(job, "http://u", tmp_path, "x.safetensors"))
    assert job.status == "done" and job.progress == 100
    assert "downloading 100%" in list(job.logs)


def test_run_download_progress_capped_99_mid_run(monkeypatch, tmp_path):
    # a "100%" line mid-run clamps to 99; only rc==0 lifts it to 100.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec(err=[b"100%\n"], rc=1))
    job = Job(id="d2", kind="download", label="c/x")
    asyncio.run(comfyui._run_download(job, "http://u", tmp_path, "x.safetensors"))
    assert job.status == "error"
    assert job.progress == 99


def test_run_download_no_pct_still_done(monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        _exec(err=[b"connecting...\n", b"done\n"], rc=0))
    job = Job(id="d3", kind="download", label="c/x")
    asyncio.run(comfyui._run_download(job, "http://u", tmp_path, "x.safetensors"))
    assert job.status == "done" and job.progress == 100  # done always lands on 100


# ---------------------------------------------------------------- generate runner (mocked httpx helpers)

def test_run_generate_collects_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(comfyui, "_OPTS", {"output_dir": str(tmp_path),
                                           "api": "http://x", "vram_gb": 24})

    async def qp(_api, _wf):
        return "pid1"

    ticks = {"n": 0}

    async def fh(_api, _pid):
        ticks["n"] += 1
        if ticks["n"] < 2:
            return None  # not finished yet → one poll cycle
        return {"outputs": {"9": {"images": [{"filename": "a.png"}]}},
                "status": {"status": "success"}}

    async def _nosleep(_t):
        return None

    monkeypatch.setattr(comfyui, "_queue_prompt", qp)
    monkeypatch.setattr(comfyui, "_fetch_history", fh)
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    job = Job(id="g1", kind="generate", label="m")
    asyncio.run(comfyui._run_generate(job, {}))
    assert job.status == "done"
    assert job.output_paths == [str(tmp_path / "a.png")]


# ---------------------------------------------------------------- gating + path logic

@pytest.fixture
def comfy_opts(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    models = tmp_path / "models"
    out = tmp_path / "out"
    for d in (checkout, models, out):
        d.mkdir()
    monkeypatch.setattr(comfyui, "_OPTS", {
        "checkout": str(checkout), "models_dir": str(models),
        "output_dir": str(out), "vram_gb": 24, "api": "http://127.0.0.1:8188",
    })
    comfyui._AVAILABLE, comfyui._AVAIL_REASON = comfyui._compute_availability()
    comfyui._JOBS.clear()
    comfyui._catalog.cache_clear()
    return {"checkout": checkout, "models": models, "out": out}


def test_generate_409_when_generation_running(comfy_opts):
    comfyui._JOBS["old"] = Job(id="old", kind="generate", label="x", status="running")
    # a download running must NOT trigger the 409 (downloads don't block generate)
    comfyui._JOBS["dl"] = Job(id="dl", kind="download", label="y", status="running")
    body = comfyui.GenerateBody(prompt="cat", model="m.safetensors", width=512, height=512)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(comfyui.generate(body))
    assert ex.value.status_code == 409


def test_download_running_does_not_block_generate_model_check(comfy_opts, monkeypatch):
    # only a generate job blocks; a download alone lets us reach the 400 (model
    # not installed), proving downloads don't gate generation.
    comfyui._JOBS["dl"] = Job(id="dl", kind="download", label="y", status="running")
    body = comfyui.GenerateBody(prompt="cat", model="nope.safetensors", width=512, height=512)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(comfyui.generate(body))
    assert ex.value.status_code == 400


def test_generate_400_unknown_model(comfy_opts):
    body = comfyui.GenerateBody(prompt="cat", model="nope.safetensors", width=512, height=512)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(comfyui.generate(body))
    assert ex.value.status_code == 400


def test_download_skips_installed_components(comfy_opts, monkeypatch):
    entry = {"id": "t", "components": [
        {"folder": "checkpoints", "filename": "a.safetensors", "url": "http://u/a", "size_gb": 1},
        {"folder": "vae", "filename": "b.safetensors", "url": "http://u/b", "size_gb": 1},
    ]}
    monkeypatch.setattr(comfyui, "_catalog", lambda: (entry,))
    ck = comfy_opts["models"] / "checkpoints"
    ck.mkdir()
    (ck / "a.safetensors").write_bytes(b"")  # a is installed → only b should queue

    async def fake_run(job, _url, _folder, _name):
        job.status = "queued"
    monkeypatch.setattr(comfyui, "_run_download", fake_run)

    out = asyncio.run(comfyui.download(comfyui.DownloadBody(entry_id="t")))
    assert len(out["job_ids"]) == 1  # only the missing vae component


def test_download_queues_all_missing_components_tagged_with_entry(comfy_opts, monkeypatch):
    # nothing installed → EVERY component gets its own job, all tagged entry_id
    entry = {"id": "multi", "components": [
        {"folder": "checkpoints", "filename": "a.safetensors", "url": "http://u/a", "size_gb": 1},
        {"folder": "text_encoders", "filename": "b.safetensors", "url": "http://u/b", "size_gb": 1},
        {"folder": "vae", "filename": "c.safetensors", "url": "http://u/c", "size_gb": 1},
    ]}
    monkeypatch.setattr(comfyui, "_catalog", lambda: (entry,))

    async def fake_run(job, _url, _folder, _name):
        job.status = "queued"
    monkeypatch.setattr(comfyui, "_run_download", fake_run)

    out = asyncio.run(comfyui.download(comfyui.DownloadBody(entry_id="multi")))
    assert len(out["job_ids"]) == 3
    for jid in out["job_ids"]:
        assert comfyui._JOBS[jid].entry_id == "multi"
    labels = {comfyui._JOBS[jid].label for jid in out["job_ids"]}
    assert labels == {"checkpoints/a.safetensors", "text_encoders/b.safetensors",
                      "vae/c.safetensors"}


def test_download_404_unknown_entry(comfy_opts, monkeypatch):
    monkeypatch.setattr(comfyui, "_catalog", lambda: ())
    with pytest.raises(HTTPException) as ex:
        asyncio.run(comfyui.download(comfyui.DownloadBody(entry_id="ghost")))
    assert ex.value.status_code == 404


def test_expand_503_without_key(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with pytest.raises(HTTPException) as ex:
        asyncio.run(comfyui.expand(comfyui.ExpandBody(idea="cat")))
    assert ex.value.status_code == 503


def test_outputs_file_rejects_escape(comfy_opts):
    with pytest.raises(HTTPException) as ex:
        comfyui.outputs_file(path="/etc/passwd")
    assert ex.value.status_code == 403


def test_scan_installed_classifies_image_and_video(comfy_opts, monkeypatch):
    monkeypatch.setattr(comfyui, "_catalog", lambda: ())
    ck = comfy_opts["models"] / "checkpoints"
    dm = comfy_opts["models"] / "diffusion_models"
    ck.mkdir()
    dm.mkdir()
    (ck / "sdxl.safetensors").write_bytes(b"")
    (dm / "hunyuan_i2v.safetensors").write_bytes(b"")
    out = comfyui._scan_installed(comfy_opts["models"])
    assert "sdxl.safetensors" in out["image"]
    assert "hunyuan_i2v.safetensors" in out["video"]  # "i2v" → video fallback


def test_scan_installed_uses_catalog_modality_first(comfy_opts, monkeypatch):
    # a catalog entry declares a video-named file as image → catalog wins
    monkeypatch.setattr(comfyui, "_catalog", lambda: (
        {"id": "x", "modality": "image",
         "components": [{"folder": "checkpoints", "filename": "weird_i2v.safetensors"}]},
    ))
    ck = comfy_opts["models"] / "checkpoints"
    ck.mkdir()
    (ck / "weird_i2v.safetensors").write_bytes(b"")
    out = comfyui._scan_installed(comfy_opts["models"])
    assert "weird_i2v.safetensors" in out["image"]


def test_catalog_endpoint_fit_and_installed(comfy_opts, monkeypatch):
    entry = {"id": "s", "name": "SDXL", "family": "sdxl", "modality": "image",
             "variant": "base", "vram_gb": 8, "proven": False,
             "components": [
                 {"folder": "checkpoints", "filename": "a.safetensors",
                  "url": "http://u/a", "size_gb": 6.9, "verified": True}]}
    monkeypatch.setattr(comfyui, "_catalog", lambda: (entry,))
    ck = comfy_opts["models"] / "checkpoints"
    ck.mkdir()
    (ck / "a.safetensors").write_bytes(b"")
    out = comfyui.catalog()
    e = out["entries"][0]
    assert e["fit"] == "ok"          # 8 <= 24
    assert e["installed"] is True
    assert e["components"][0]["installed"] is True
    assert "verified" not in e["components"][0]   # verified is YAML-only


def test_catalog_endpoint_hides_entries_that_dont_fit(comfy_opts, monkeypatch):
    # machine vram is 24 → ok(8) stays; warn(25) and no(40) are dropped entirely.
    def entry(i, vram):
        return {"id": i, "name": i, "vram_gb": vram, "components": []}
    monkeypatch.setattr(
        comfyui, "_catalog",
        lambda: (entry("fits", 8), entry("offload", 25), entry("too-big", 40)))
    out = comfyui.catalog()
    assert [e["id"] for e in out["entries"]] == ["fits"]
    assert out["entries"][0]["fit"] == "ok"


def test_entry_view_propagates_unrestricted_flag(comfy_opts):
    md, mv = comfy_opts["models"], 24
    spicy = {"id": "p", "vram_gb": 10, "unrestricted": True, "components": []}
    plain = {"id": "s", "vram_gb": 10, "components": []}
    assert comfyui._entry_view(spicy, md, mv)["unrestricted"] is True
    assert comfyui._entry_view(plain, md, mv)["unrestricted"] is False  # default


# ---------------------------------------------------------------- entry-level download cancel

def test_cancel_download_cancels_only_that_entrys_live_jobs(comfy_opts):
    comfyui._JOBS["q1"] = Job(id="q1", kind="download", label="checkpoints/a",
                              entry_id="t", status="queued")
    comfyui._JOBS["r1"] = Job(id="r1", kind="download", label="vae/b",
                              entry_id="t", status="running")
    comfyui._JOBS["d1"] = Job(id="d1", kind="download", label="loras/c",
                              entry_id="t", status="done")       # finished — untouched
    comfyui._JOBS["o1"] = Job(id="o1", kind="download", label="checkpoints/x",
                              entry_id="other", status="running")  # other entry — untouched
    comfyui._JOBS["g1"] = Job(id="g1", kind="generate", label="m", status="running")
    out = asyncio.run(comfyui.cancel_download(comfyui.CancelDownloadBody(entry_id="t")))
    assert sorted(out["cancelled"]) == ["q1", "r1"]
    assert comfyui._JOBS["q1"].cancel and comfyui._JOBS["r1"].cancel
    assert not comfyui._JOBS["d1"].cancel
    assert not comfyui._JOBS["o1"].cancel
    assert not comfyui._JOBS["g1"].cancel


def test_cancel_download_idempotent_when_nothing_live(comfy_opts):
    out = asyncio.run(comfyui.cancel_download(comfyui.CancelDownloadBody(entry_id="ghost")))
    assert out == {"cancelled": []}


def test_run_download_cancelled_while_queued_never_spawns(monkeypatch, tmp_path):
    async def boom(*_a, **_k):  # spawning after cancel would be a bug
        raise AssertionError("subprocess spawned for a cancelled job")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    job = Job(id="c1", kind="download", label="c/x", entry_id="t", cancel=True)
    asyncio.run(comfyui._run_download(job, "http://u", tmp_path, "x.safetensors"))
    assert job.status == "cancelled"


# ---------------------------------------------------------------- delete / uninstall

def test_delete_removes_installed_component_files(comfy_opts, monkeypatch):
    entry = {"id": "t", "components": [
        {"folder": "checkpoints", "filename": "a.safetensors", "url": "http://u/a", "size_gb": 1},
        {"folder": "vae", "filename": "b.safetensors", "url": "http://u/b", "size_gb": 1},
    ]}
    monkeypatch.setattr(comfyui, "_catalog", lambda: (entry,))
    ck = comfy_opts["models"] / "checkpoints"
    ck.mkdir()
    vae = comfy_opts["models"] / "vae"
    vae.mkdir()
    a = ck / "a.safetensors"
    a.write_bytes(b"x")
    b = vae / "b.safetensors"
    b.write_bytes(b"y")
    out = asyncio.run(comfyui.delete(comfyui.DeleteBody(entry_id="t")))
    assert not a.exists() and not b.exists()
    assert len(out["deleted"]) == 2


def test_delete_404_unknown_entry(comfy_opts, monkeypatch):
    monkeypatch.setattr(comfyui, "_catalog", lambda: ())
    with pytest.raises(HTTPException) as ex:
        asyncio.run(comfyui.delete(comfyui.DeleteBody(entry_id="ghost")))
    assert ex.value.status_code == 404


def test_delete_skips_missing_and_refuses_path_escape(comfy_opts, monkeypatch):
    # missing file is skipped (no error); a folder escaping models_dir is refused.
    entry = {"id": "t", "components": [
        {"folder": "checkpoints", "filename": "gone.safetensors", "url": "http://u", "size_gb": 1},
        {"folder": "../../etc", "filename": "passwd", "url": "http://u", "size_gb": 1},
    ]}
    monkeypatch.setattr(comfyui, "_catalog", lambda: (entry,))
    ck = comfy_opts["models"] / "checkpoints"
    ck.mkdir()
    out = asyncio.run(comfyui.delete(comfyui.DeleteBody(entry_id="t")))
    assert out["deleted"] == []
