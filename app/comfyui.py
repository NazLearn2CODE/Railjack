"""ComfyUI panel — model catalog + async download/generate job runner + REST.

Mirrors ``app/ffmpeg_jobs.py``: module options resolved from the ``comfyui``
panel at import (``_OPTS``), an asyncio job store (``_JOBS`` / ``_BG`` /
deque logs / cancel), pydantic request models, argv **lists** run via
``asyncio.create_subprocess_exec`` (never shell), and ``os.path.commonpath``
confinement for every client-supplied path.

Concurrency rule: at most one ``generate`` job queued/running (409 otherwise);
``download`` jobs run concurrently with each other AND with a generation.
Service start/stop is NOT here — the frontend uses ``/api/modules/comfyui/*``.
"""

from __future__ import annotations

import asyncio
import functools
import os
import random
import re
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import CONFIG
from .zai import zai_message

router = APIRouter()

# ---------------------------------------------------------------- module options

FETCH_MODEL = "/home/NAZ/.claude/skills/f5-comfyui-media/scripts/fetch_model.py"
# catalog/ subdir keeps this out of config.py's configs/*.yaml machine-config glob
CATALOG_YAML = Path(__file__).resolve().parent.parent / "configs" / "catalog" / "comfyui.yaml"

_MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin"}
_OUT_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".mp4"}


def _comfyui_options() -> dict:
    for m in CONFIG.modules:
        if m.kind == "panel" and m.panel == "comfyui":
            return m.options or {}
    return {}


_OPTS = _comfyui_options()


def _models_dir() -> Path:
    return Path(_OPTS["models_dir"]).expanduser().resolve()


def _output_dir() -> Path:
    return Path(_OPTS["output_dir"]).expanduser().resolve()


def _api() -> str:
    return _OPTS.get("api", "http://127.0.0.1:8188")


def _machine_vram() -> float:
    return float(_OPTS.get("vram_gb", 0) or 0)


def _compute_availability() -> tuple[bool, str | None]:
    """available = checkout dir exists AND vram_gb > 0 (cached at import)."""
    co = _OPTS.get("checkout")
    if not co or not Path(co).expanduser().is_dir():
        return False, f"checkout dir not found: {co!r}"
    if _machine_vram() <= 0:
        return False, "vram_gb not configured"
    return True, None


_AVAILABLE, _AVAIL_REASON = _compute_availability()


# ---------------------------------------------------------------- path safety


def _under(path: Path, roots: list[Path]) -> bool:
    """True if ``path`` (already resolved) lives at or under one of ``roots``."""
    for root in roots:
        try:
            if os.path.commonpath([str(path), str(root)]) == str(root):
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------- catalog


@functools.lru_cache(maxsize=1)
def _catalog() -> tuple[dict, ...]:
    """Raw catalog entries (immutable tuple so lru_cache is safe)."""
    if not CATALOG_YAML.is_file():
        return ()
    data = yaml.safe_load(CATALOG_YAML.read_text()) or {}
    return tuple(data.get("entries", []) or [])


def _fit(entry_vram: float, machine: float) -> str:
    """ok if <= machine vram; warn if <= 1.2× (offload territory); else no."""
    if entry_vram <= machine:
        return "ok"
    if entry_vram <= machine * 1.2 + 1e-9:  # epsilon: 24*1.2 is 28.799…97 in float
        return "warn"
    return "no"


# ComfyUI merges clip/ and text_encoders/ into one loader path — check both.
_FOLDER_ALIASES = {"text_encoders": ("text_encoders", "clip"), "clip": ("clip", "text_encoders")}


def _component_installed(models_dir: Path, folder: str, filename: str) -> bool:
    for f in _FOLDER_ALIASES.get(folder, (folder,)):
        if (models_dir / f / filename).is_file():
            return True
    return False


def _entry_view(e: dict, models_dir: Path, machine: float) -> dict:
    comps, all_in, size = [], True, 0.0
    for c in e.get("components", []):
        inst = _component_installed(models_dir, c["folder"], c["filename"])
        all_in = all_in and inst
        size += float(c.get("size_gb", 0) or 0)
        comps.append({
            "folder": c["folder"], "filename": c["filename"], "url": c["url"],
            "size_gb": c.get("size_gb", 0), "installed": inst,
        })
    return {
        "id": e["id"],
        "name": e.get("name", e["id"]),
        "family": e.get("family"),
        "modality": e.get("modality"),
        "variant": e.get("variant"),
        "vram_gb": e.get("vram_gb"),
        "proven": e.get("proven", False),
        "installed": bool(comps) and all_in,
        "fit": _fit(float(e.get("vram_gb", 0) or 0), machine),
        "size_gb": round(size, 2),
        "components": comps,
    }


def _entry_for_model(model: str, catalog) -> dict | None:
    """Catalog entry whose checkpoint/diffusion component filename == model."""
    for e in catalog:
        for c in e.get("components", []):
            if c.get("filename") == model and c.get("folder") in ("checkpoints", "diffusion_models"):
                return e
    return None


def _scan_installed(models_dir: Path) -> dict:
    """Checkpoint/diffusion filenames grouped by modality. Classification: catalog
    match first, else filename heuristic (video/i2v/t2v → video)."""
    fmap: dict[str, str] = {}
    for e in _catalog():
        for c in e.get("components", []):
            fmap[c["filename"]] = e.get("modality")
    files: list[str] = []
    for sub in ("checkpoints", "diffusion_models", "unet"):
        d = models_dir / sub
        if d.is_dir():
            files += (p.name for p in d.iterdir()
                      if p.is_file() and p.suffix.lower() in _MODEL_EXTS)
    image: list[str] = []
    video: list[str] = []
    for name in sorted(set(files)):
        mod = fmap.get(name)
        if mod is None:
            low = name.lower()
            mod = "video" if any(t in low for t in ("video", "i2v", "t2v")) else "image"
        (video if mod == "video" else image).append(name)
    return {"image": image, "video": video}


# ---------------------------------------------------------------- workflow builders
# Proven krea2 sampler values — do NOT change (per spec). The generic variant
# reuses them ("same graph with ckpt_name swapped").
_STEPS, _CFG, _SAMPLER, _SCHED = 4, 1.5, "euler", "simple"


def _generic_t2i(ckpt, prompt, negative, width, height, seed, batch) -> dict:
    """Standard SD/SDXL txt2i: CLIP + VAE come from CheckpointLoaderSimple
    (slots 1/2) — "CLIP from the checkpoint instead of the separate CLIPLoader"."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "", "clip": ["4", 1]}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": batch or 1}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": _STEPS, "cfg": _CFG, "sampler_name": _SAMPLER,
            "scheduler": _SCHED, "denoise": 1.0, "model": ["4", 0],
            "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "railjack"}},
    }


def _flow_t2i(ckpt, clip_name, vae_name, prompt, negative, width, height, seed, batch) -> dict:
    """krea2 graph — matches the skill's proven gen_image.py build() (CLIPLoader
    type "krea2", EmptySD3LatentImage; do not "improve" the sampler values)."""
    return {
        "10": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "11": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": clip_name, "type": "krea2"}},
        "12": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
        "13": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["11", 0]}},
        "14": {"class_type": "CLIPTextEncode", "inputs": {"text": negative or "", "clip": ["11", 0]}},
        "15": {"class_type": "EmptySD3LatentImage",
               "inputs": {"width": width, "height": height, "batch_size": batch or 1}},
        "16": {"class_type": "KSampler", "inputs": {
            "seed": seed, "control_after_generate": "fixed",
            "steps": _STEPS, "cfg": _CFG, "sampler_name": _SAMPLER,
            "scheduler": _SCHED, "denoise": 1.0, "model": ["10", 0],
            "positive": ["13", 0], "negative": ["14", 0], "latent_image": ["15", 0]}},
        "17": {"class_type": "VAEDecode", "inputs": {"samples": ["16", 0], "vae": ["12", 0]}},
        "18": {"class_type": "SaveImage", "inputs": {"images": ["17", 0], "filename_prefix": "railjack"}},
    }


def _workflow(model, prompt, negative, width, height, seed, batch, catalog) -> dict:
    """Flow variant (separate CLIP+VAE loaders) when the model's entry ships them;
    generic CheckpointLoaderSimple graph otherwise."""
    e = _entry_for_model(model, catalog)
    clip = vae = None
    if e:
        for c in e.get("components", []):
            if c.get("folder") == "text_encoders":
                clip = c["filename"]
            elif c.get("folder") == "vae":
                vae = c["filename"]
    if clip and vae:
        return _flow_t2i(model, clip, vae, prompt, negative, width, height, seed, batch)
    return _generic_t2i(model, prompt, negative, width, height, seed, batch)


# ---------------------------------------------------------------- job state

@dataclass
class Job:
    id: str
    kind: str  # "download" | "generate"
    label: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: int = 0
    output_paths: list[str] = field(default_factory=list)
    error: str | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=200))
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    cancel: bool = False
    prompt_id: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "progress": self.progress, "output_paths": self.output_paths,
            "error": self.error, "logs": list(self.logs),
        }


_JOBS: dict[str, Job] = {}
_RUNNING = {"queued", "running"}
_BG: set[asyncio.Task[object]] = set()


# ---------------------------------------------------------------- runners

_PCT = re.compile(r"(\d+)%")


async def _run_download(job: Job, url: str, folder: Path, filename: str) -> None:
    argv = ["python3", FETCH_MODEL, url, "--folder", str(folder), "--name", filename]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        job.status, job.error = "error", "python3 not found on PATH"
        return
    job.proc, job.status = proc, "running"

    async def pump(stream, name: str) -> None:
        assert stream is not None
        while True:
            line = await stream.readline()
            if not line:
                break
            s = line.decode(errors="replace").rstrip()
            job.logs.append(s)
            m = _PCT.search(s)  # curl-style "%" progress, when fetch_model emits it
            if m:
                job.progress = max(0, min(99, int(m.group(1))))

    await asyncio.gather(pump(proc.stdout, "out"), pump(proc.stderr, "err"))
    rc = await proc.wait()
    job.proc = None
    if job.cancel or rc in (-signal.SIGTERM, -signal.SIGINT):
        job.status = "cancelled"
    elif rc == 0:
        job.status, job.progress = "done", 100
    else:
        job.status, job.error = "error", f"fetch_model exited {rc}"


async def _queue_prompt(api: str, workflow: dict) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{api}/prompt", json={"prompt": workflow})
        r.raise_for_status()
        return r.json()["prompt_id"]


async def _fetch_history(api: str, prompt_id: str) -> dict | None:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{api}/history/{prompt_id}")
        r.raise_for_status()
        return (r.json() or {}).get(prompt_id)


async def _run_generate(job: Job, workflow: dict) -> None:
    job.status = "running"
    api = _api()
    try:
        pid = await _queue_prompt(api, workflow)
        job.prompt_id = pid
        while True:
            if job.cancel:
                try:
                    async with httpx.AsyncClient(timeout=5) as c:
                        await c.post(f"{api}/interrupt")
                except httpx.HTTPError:
                    pass
                job.status = "cancelled"
                return
            hist = await _fetch_history(api, pid)
            if hist:
                st = (hist.get("status") or {}).get("status")
                if st == "error":
                    job.status, job.error = "error", "ComfyUI execution error"
                    return
                outs: list[str] = []
                for node in (hist.get("outputs") or {}).values():
                    for img in node.get("images", []) or []:
                        if img.get("filename"):
                            outs.append(str(_output_dir() / img["filename"]))
                job.output_paths = outs
                job.status, job.progress = "done", 100
                return
            await asyncio.sleep(2)  # ponytail: unbounded poll loop; the UI cancels
    except (httpx.HTTPError, KeyError, ValueError) as e:
        job.status, job.error = "error", str(e)


# ---------------------------------------------------------------- API models


class DownloadBody(BaseModel):
    entry_id: str


class DeleteBody(BaseModel):
    entry_id: str


class GenerateBody(BaseModel):
    prompt: str
    negative: str | None = None
    model: str
    width: int
    height: int
    seed: int | None = None
    batch: int | None = None


class ExpandBody(BaseModel):
    idea: str
    style: str | None = None
    aspect: str | None = None


# ---------------------------------------------------------------- endpoints


@router.get("/api/comfyui/state")
async def state() -> dict:
    out: dict = {"available": _AVAILABLE, "reason": _AVAIL_REASON,
                 "up": False, "vram_free_gb": None, "vram_total_gb": None}
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{_api()}/system_stats")
            r.raise_for_status()
            devs = (r.json() or {}).get("devices") or []
            if devs:
                d = devs[0]
                total = round((d.get("vram_total") or 0) / 1024 ** 3, 2)
                free = round((d.get("vram_free") or 0) / 1024 ** 3, 2)
                out["vram_total_gb"] = total or None
                out["vram_free_gb"] = free or None
                out["up"] = True
    except (httpx.HTTPError, ValueError):
        pass
    return out


@router.get("/api/comfyui/catalog")
def catalog() -> dict:
    md, mv = _models_dir(), _machine_vram()
    return {"entries": [_entry_view(e, md, mv) for e in _catalog()]}


@router.get("/api/comfyui/installed")
def installed() -> dict:
    return _scan_installed(_models_dir())


@router.post("/api/comfyui/download")
async def download(body: DownloadBody) -> dict:
    e = next((x for x in _catalog() if x["id"] == body.entry_id), None)
    if not e:
        raise HTTPException(404, "unknown catalog entry")
    models_dir = _models_dir()
    ids: list[str] = []
    for c in e.get("components", []):
        if _component_installed(models_dir, c["folder"], c["filename"]):
            continue  # already installed — skip
        folder = (models_dir / c["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        jid = uuid4().hex[:8]
        job = Job(id=jid, kind="download", label=f"{c['folder']}/{c['filename']}")
        _JOBS[jid] = job
        task = asyncio.create_task(_run_download(job, c["url"], folder, c["filename"]))
        _BG.add(task)
        task.add_done_callback(_BG.discard)
        ids.append(jid)
    return {"job_ids": ids}


@router.post("/api/comfyui/delete")
async def delete(body: DeleteBody) -> dict:
    """Uninstall an entry: delete its installed component files (catalog stays)."""
    e = next((x for x in _catalog() if x["id"] == body.entry_id), None)
    if not e:
        raise HTTPException(404, "unknown catalog entry")
    models_dir = _models_dir()
    deleted: list[str] = []
    for c in e.get("components", []):
        for folder in _FOLDER_ALIASES.get(c["folder"], (c["folder"],)):
            p = (models_dir / folder / c["filename"]).resolve()
            if _under(p, [models_dir]) and p.is_file():
                p.unlink()
                deleted.append(str(p))
    return {"deleted": deleted}


@router.post("/api/comfyui/generate")
async def generate(body: GenerateBody) -> dict:
    if any(j.kind == "generate" and j.status in _RUNNING for j in _JOBS.values()):
        raise HTTPException(409, "a generation is already queued/running")
    installed = _scan_installed(_models_dir())
    if body.model not in {*installed["image"], *installed["video"]}:
        raise HTTPException(400, f"model {body.model!r} not installed")
    seed = body.seed if body.seed is not None else random.randint(0, 2 ** 32 - 1)
    wf = _workflow(body.model, body.prompt, body.negative, body.width, body.height,
                   seed, body.batch or 1, _catalog())
    jid = uuid4().hex[:8]
    job = Job(id=jid, kind="generate", label=body.model)
    _JOBS[jid] = job
    task = asyncio.create_task(_run_generate(job, wf))
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return {"id": jid}


@router.post("/api/comfyui/expand")
async def expand(body: ExpandBody) -> dict:
    style = body.style or "cinematic photographic"
    msg = (f"Expand this idea into a rich Stable-Diffusion image prompt: {body.idea!r}. "
           f"Style: {style}. Cover subject, mood, lighting, composition, and style tags. "
           "One paragraph, no preamble.")
    return {"prompt": await zai_message(msg)}


@router.get("/api/comfyui/outputs")
def outputs(limit: int = 24) -> dict:
    d = _output_dir()
    items: list[tuple[float, Path]] = []
    if d.is_dir():
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in _OUT_EXTS:
                items.append((p.stat().st_mtime, p))
    items.sort(reverse=True)
    return {"files": [{"name": p.name, "path": str(p), "mtime": int(mt)}
                       for mt, p in items[: max(1, limit)]]}


@router.get("/api/comfyui/outputs/file")
def outputs_file(path: str) -> FileResponse:
    root = _output_dir()
    p = Path(path).expanduser().resolve()
    if not _under(p, [root]):
        raise HTTPException(403, "path outside output_dir")
    if not p.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(p)


@router.get("/api/comfyui/jobs")
def jobs() -> dict:
    return {"jobs": [j.to_dict() for j in reversed(list(_JOBS.values()))]}


@router.post("/api/comfyui/jobs/{jid}/cancel")
async def cancel_job(jid: str) -> dict:
    j = _JOBS.get(jid)
    if not j:
        raise HTTPException(404, "unknown job")
    if j.status not in _RUNNING:
        raise HTTPException(409, f"job is {j.status}, nothing to cancel")
    j.cancel = True
    if j.kind == "generate" and j.prompt_id:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{_api()}/interrupt")
        except httpx.HTTPError:
            pass
    if j.proc is not None:
        j.proc.send_signal(signal.SIGTERM)
    return {"status": "cancelling"}
