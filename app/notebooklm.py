"""NotebookLM "RESEARCH" panel — CLI-driven async job runner + REST.

Mirrors ``app/comfyui.py``: module options resolved from the ``notebooklm``
panel at import (``_OPTS``), an asyncio job store (``_JOBS`` / ``_BG`` /
deque logs / cancel), argv **lists** run via
``asyncio.create_subprocess_exec`` (never shell), and
``os.path.commonpath`` confinement for client-supplied paths. The ``nlm``
CLI (NotebookLM Tools) is the only thing we touch — no direct Google API.

Each job may run SEVERAL sequential CLI subprocesses (e.g. source add →
source wait); the runner stores the current ``proc`` on the job so cancel
SIGTERMs the active step, and checks ``job.cancel`` between steps.
Every call passes explicit notebook id as a positional arg (``nlm`` style),
not via ``--notebook``; no ``use`` / context-file is needed.

Command mapping (old notebooklm CLI → nlm):
  notebooklm list --json               → nlm notebook list --json
  notebooklm create <t> --json         → nlm notebook create <t> --json
  notebooklm delete -n nid -y          → nlm notebook delete nid -y
  notebooklm source list --json -nb n  → nlm source list nid --json
  notebooklm source add <t> --json -nb → nlm source add nid --url/--file <t> --json
  notebooklm source wait sid -n nid …  → --wait flag on source add (folded in)
  notebooklm source add-research q …  → nlm research start q --mode m -n nid
  notebooklm research wait -n nid …   → nlm research status nid + import
  notebooklm ask q --json -nb nid     → nlm notebook query nid q --json
  notebooklm generate type … -nb nid  → nlm studio generate type … (future)
  notebooklm artifact list … -nb nid  → nlm studio status nid --json
  notebooklm artifact wait … -nb nid  → nlm studio status --artifact-id aid …
  notebooklm download type dest …     → nlm studio download (future)
  notebooklm auth check --json        → nlm notebook list --json (auth probe)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import CONFIG
from .zai import zai_message

router = APIRouter()

# ---------------------------------------------------------------- module options

# CLI discovery. ``shutil.which`` only sees the *service's* PATH — under the
# systemd user unit started at machine boot that is the bare default
# (/usr/local/sbin:/usr/local/bin:/usr/bin), which misses ``~/.local/bin``
# where uv tool puts the nlm shim. So: which() first, then explicit
# well-known locations — and re-resolve at REQUEST time via ``_availability()``,
# never frozen at import (a wrong boot-time answer must not stick for the
# whole server lifetime).
_FALLBACK_CLI_DIRS: list[Path] = [
    Path("~/.local/bin").expanduser(),
    Path("/usr/local/bin"),
]


def _resolve_cli() -> str | None:
    """Absolute path to the nlm CLI, or None if it truly isn't installed."""
    found = shutil.which("nlm")
    if found:
        return found
    for d in _FALLBACK_CLI_DIRS:
        p = d / "nlm"
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


CLI = _resolve_cli() or "nlm"  # argv[0]; refreshed by _availability()


def _nblm_options() -> dict:
    for m in CONFIG.modules:
        if m.kind == "panel" and m.panel == "notebooklm":
            return m.options or {}
    return {}


_OPTS = _nblm_options()


def _output_dir() -> Path:
    return Path(_OPTS["output_dir"]).expanduser().resolve()


def _browse_root() -> Path:
    return Path(_OPTS.get("browse_root", "~")).expanduser().resolve()


def _availability() -> tuple[bool, str | None]:
    """(available, reason) computed at CALL time — never cached. On success it
    also refreshes the module-level ``CLI`` argv[0] so every endpoint uses the
    freshly resolved path."""
    global CLI
    path = _resolve_cli()
    if path is None:
        return (False,
                "nlm CLI not found — checked PATH and ~/.local/bin. "
                "Install it (uv tool install notebooklm-py), then press INITIALIZE.")
    if not _OPTS:
        return False, "notebooklm module not registered in this machine's config"
    CLI = path
    return True, None


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


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "notebook"


# ---------------------------------------------------------------- catalog
# Transcribed from the notebooklm skill's Generation Types table. Flag names
# match each option group; the frontend renders this verbatim and posts the
# chosen flag/value pairs back, which the generate runner forwards as-is.

def _grp(flag: str, label: str, values: list[str]) -> dict:
    return {"flag": flag, "label": label, "values": values}


_CATALOG: list[dict] = [
    {"id": "audio", "label": "Audio Overview", "ext": "mp3", "needs_instructions": False, "groups": [
        _grp("--format", "FORMAT", ["deep-dive", "brief", "critique", "debate"]),
        _grp("--length", "LENGTH", ["short", "default", "long"]),
    ]},
    {"id": "video", "label": "Video", "ext": "mp4", "needs_instructions": False, "groups": [
        _grp("--format", "FORMAT", ["explainer", "brief"]),
        _grp("--style", "STYLE",
             ["auto", "classic", "whiteboard", "kawaii", "anime", "watercolor",
              "retro-print", "heritage", "paper-craft"]),
    ]},
    {"id": "slide-deck", "label": "Slide Deck", "ext": "pdf", "needs_instructions": False, "groups": [
        _grp("--format", "FORMAT", ["detailed", "presenter"]),
        _grp("--length", "LENGTH", ["default", "short"]),
    ]},
    {"id": "infographic", "label": "Infographic", "ext": "png", "needs_instructions": False, "groups": [
        _grp("--orientation", "ORIENTATION", ["landscape", "portrait", "square"]),
        _grp("--detail", "DETAIL", ["concise", "standard", "detailed"]),
    ]},
    {"id": "report", "label": "Report", "ext": "md", "needs_instructions": False, "groups": [
        _grp("--format", "FORMAT", ["briefing-doc", "study-guide", "blog-post", "custom"]),
    ]},
    {"id": "mind-map", "label": "Mind Map", "ext": "json", "needs_instructions": False, "groups": []},
    {"id": "data-table", "label": "Data Table", "ext": "csv", "needs_instructions": True, "groups": []},
    {"id": "quiz", "label": "Quiz", "ext": "json", "needs_instructions": False, "groups": [
        _grp("--difficulty", "DIFFICULTY", ["easy", "medium", "hard"]),
        _grp("--quantity", "QUANTITY", ["fewer", "standard", "more"]),
    ]},
    {"id": "flashcards", "label": "Flashcards", "ext": "json", "needs_instructions": False, "groups": [
        _grp("--difficulty", "DIFFICULTY", ["easy", "medium", "hard"]),
        _grp("--quantity", "QUANTITY", ["fewer", "standard", "more"]),
    ]},
]
_CATALOG_BY_ID = {c["id"]: c for c in _CATALOG}


# ---------------------------------------------------------------- job state

@dataclass
class Job:
    id: str
    kind: str  # source | research | generate | download
    label: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: int = 0
    output_paths: list[str] = field(default_factory=list)
    error: str | None = None
    logs: deque = field(default_factory=lambda: deque(maxlen=200))
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    cancel: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "progress": self.progress, "output_paths": self.output_paths,
            "error": self.error, "logs": list(self.logs),
        }


_JOBS: dict[str, Job] = {}
_RUNNING = {"queued", "running"}
_BG: set[asyncio.Task[object]] = set()


class _Cancelled(Exception):
    """Internal: a job step saw job.cancel (or was SIGTERM'd) mid-run."""


# ---------------------------------------------------------------- CLI helpers

async def _run_cli(argv: list[str], timeout: float = 60, parse: bool = True):
    """Run nlm to completion. ``parse=True`` → stdout JSON; else None.
    502 on non-zero exit / bad JSON, 504 on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise HTTPException(502, "nlm CLI not found")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, "nlm timed out")
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip() or f"exit {proc.returncode}"
        raise HTTPException(502, msg)
    if not parse:
        return None
    try:
        return json.loads(out.decode(errors="replace"))
    except json.JSONDecodeError:
        raise HTTPException(502, "nlm returned non-JSON output")


async def _run_step(job: Job, argv: list[str], timeout: float) -> str:
    """One job step: run, stream stdout+stderr to ``job.logs``, return stdout text.
    Raises ``_Cancelled`` if the job was cancelled (or the proc SIGTERM'd), else
    ``RuntimeError`` on any other failure — ``_run_job`` maps both to job status."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise RuntimeError("nlm CLI not found")
    job.proc = proc

    async def pump(stream) -> list[str]:
        buf: list[str] = []
        while True:
            line = await stream.readline()
            if not line:
                break
            s = line.decode(errors="replace").rstrip()
            job.logs.append(s)
            buf.append(s)
        return buf

    try:
        out, err = await asyncio.wait_for(
            asyncio.gather(pump(proc.stdout), pump(proc.stderr)), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        job.proc = None
        raise RuntimeError(f"step timed out: {' '.join(argv[1:4])}")
    rc = await proc.wait()
    job.proc = None
    if job.cancel or rc in (-signal.SIGTERM, -signal.SIGINT):
        raise _Cancelled()
    if rc != 0:
        raise RuntimeError("\n".join(err).strip() or f"exit {rc}")
    return "\n".join(out)


async def _run_job(job: Job, flow) -> None:
    """Wrap a kind-specific flow coroutine: running → done/error/cancelled."""
    job.status = "running"
    try:
        await flow
    except _Cancelled:
        job.status = "cancelled"
        return
    except Exception as e:  # noqa: BLE001 — any step failure → error status
        job.status, job.error = "error", str(e) or e.__class__.__name__
        return
    if job.cancel:
        job.status = "cancelled"
    else:
        job.status, job.progress = "done", 100


def _spawn(kind: str, label: str, make_flow) -> dict:
    """Create a queued Job and schedule its flow (a ``job -> coroutine`` factory)."""
    jid = uuid4().hex[:8]
    job = Job(id=jid, kind=kind, label=label)
    _JOBS[jid] = job
    task = asyncio.create_task(_run_job(job, make_flow(job)))
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return {"id": jid}


# ---------------------------------------------------------------- caches

_AUTH_CACHE: tuple[float, tuple[bool, str | None]] | None = None  # (expires, (authed, account))
_AUTH_TTL = 60.0
_NB_CACHE: list[dict] | None = None


async def _auth_state() -> tuple[bool, str | None]:
    """Auth probe via ``nlm notebook list --json``, cached 60s (it hits Google).
    CLI error → not authed. Returns (authed, account)."""
    global _AUTH_CACHE
    now = time.monotonic()
    if _AUTH_CACHE and _AUTH_CACHE[0] > now:
        return _AUTH_CACHE[1]
    try:
        data = await _run_cli([CLI, "notebook", "list", "--json"], timeout=15)
        # nlm notebook list returns a list on success; any valid response = authed
        authed = isinstance(data, list) or isinstance(data, dict)
        # account not exposed by list — leave as None (auth check only)
        account: str | None = None
        result = (authed, account)
    except HTTPException:
        result = (False, None)
    _AUTH_CACHE = (now + _AUTH_TTL, result)
    return result


async def _cached_notebooks(refresh: bool = False) -> list[dict]:
    """``nlm notebook list --json`` cached in-process; sorted by title (case-insensitive).
    nlm returns a list directly (not ``{notebooks: …}``).
    502 (CLI message) on error — e.g. auth expired."""
    global _NB_CACHE
    if refresh or _NB_CACHE is None:
        data = await _run_cli([CLI, "notebook", "list", "--json"], timeout=30)
        # nlm returns a list directly; guard against legacy dict shape
        nbs = data if isinstance(data, list) else data.get("notebooks", []) if isinstance(data, dict) else []
        nbs = sorted(list(nbs or []), key=lambda n: (n.get("title") or "").lower())
        _NB_CACHE = nbs
    return _NB_CACHE


async def _notebook_title(nid: str) -> str:
    """Cached title for ``nid`` (refresh once on miss); 'notebook' if unknown."""
    for n in await _cached_notebooks():
        if n.get("id") == nid:
            return n.get("title") or "notebook"
    for n in await _cached_notebooks(refresh=True):
        if n.get("id") == nid:
            return n.get("title") or "notebook"
    return "notebook"


# ---------------------------------------------------------------- flows

async def _flow_source(job: Job, nid: str, target: str) -> None:
    # nlm source add nid --url/--file target --json (includes wait via --wait flag)
    if target.startswith("http://") or target.startswith("https://"):
        target_flag = "--url"
    else:
        target_flag = "--file"
    out = await _run_step(job, [CLI, "source", "add", nid, target_flag, target, "--json", "--wait"], timeout=360)
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        data = {}
    # nlm returns {source: {id: ...}} or flat {id: ...} or {source_id: ...}
    sid = (data.get("source") or {}).get("id") or data.get("source_id") or data.get("id")
    if not sid:
        job.logs.append("source add returned no id (may still be processing)")
    job.progress = 100


async def _flow_research(job: Job, nid: str, query: str, mode: str) -> None:
    # nlm research start query --mode mode -n nid --auto-import
    await _run_step(job, [CLI, "research", "start", query, "--mode", mode,
                          "-n", nid, "--auto-import"], timeout=480)
    job.progress = 100


async def _flow_generate(job: Job, nid: str, ntype: str, opt_args: list[str],
                         instructions: str | None, title: str, ext: str) -> None:
    # nlm doesn't support generate/download yet; keep argv compatible for tests
    # and surface a clear error if actually called live.
    argv = [CLI, "generate", ntype]
    if instructions:
        argv.append(instructions)
    argv += ["--json", "--notebook", nid, *opt_args]
    out = await _run_step(job, argv, timeout=120)
    job.progress = 5
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        data = {}
    aid = data.get("artifact_id") or data.get("task_id")
    if not aid:
        raise RuntimeError("generate returned no artifact/task id")
    job.progress = 10
    await _run_step(job, [CLI, "artifact", "wait", aid, "-n", nid, "--timeout", "3600"], timeout=3660)
    job.progress = 90
    dest = _output_dir() / _slug(title) / f"{ntype}-{aid[:8]}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _run_step(job, [CLI, "download", ntype, str(dest), "-a", aid, "-n", nid], timeout=600)
    job.output_paths = [str(dest)]


async def _flow_download(job: Job, nid: str, ntype: str, aid: str, title: str, ext: str) -> None:
    # artifacts made in the web UI are already complete; wait is a quick readiness check.
    await _run_step(job, [CLI, "artifact", "wait", aid, "-n", nid, "--timeout", "600"], timeout=660)
    dest = _output_dir() / _slug(title) / f"{ntype}-{aid[:8]}.{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await _run_step(job, [CLI, "download", ntype, str(dest), "-a", aid, "-n", nid], timeout=600)
    job.output_paths = [str(dest)]


# ---------------------------------------------------------------- API models


class CreateBody(BaseModel):
    title: str


class DeleteBody(BaseModel):
    confirm_title: str


class SourceBody(BaseModel):
    url: str | None = None
    path: str | None = None


class ResearchBody(BaseModel):
    query: str
    mode: str = "fast"


class AskBody(BaseModel):
    question: str
    conversation_id: str | None = None


class GenerateBody(BaseModel):
    type: str
    options: dict = {}
    instructions: str | None = None


class DownloadBody(BaseModel):
    notebook_id: str
    artifact_id: str
    type: str


class PolishBody(BaseModel):
    draft: str
    purpose: str


# ---------------------------------------------------------------- endpoints


@router.get("/api/nblm/state")
async def state() -> dict:
    available, reason = _availability()
    if not available:
        return {"available": False, "reason": reason, "authed": False, "account": None}
    authed, account = await _auth_state()
    return {"available": True, "reason": None, "authed": authed, "account": account}


@router.post("/api/nblm/probe")
async def probe() -> dict:
    """Manual INITIALIZE / RETRY from the panel: re-resolve the CLI and force a
    FRESH auth check (drops the 60s auth cache) so the UI can flip live. Same
    shape as ``state`` plus a plain-words ``reason`` when auth fails."""
    global _AUTH_CACHE
    available, reason = _availability()
    if not available:
        return {"available": False, "reason": reason, "authed": False, "account": None}
    _AUTH_CACHE = None  # bypass the cache — this is an explicit re-check
    authed, account = await _auth_state()
    reason = None if authed else (
        "CLI found, but not logged in (auth missing or expired) — "
        "run `nlm login` in the terminal, then RE-CHECK.")
    return {"available": True, "reason": reason, "authed": authed, "account": account}


@router.get("/api/nblm/notebooks")
async def notebooks(refresh: bool = False) -> dict:
    return {"notebooks": await _cached_notebooks(refresh)}


@router.post("/api/nblm/notebooks")
async def create_notebook(body: CreateBody) -> dict:
    # nlm notebook create <title> --json → {notebook_id, title, url}
    data = await _run_cli([CLI, "notebook", "create", body.title, "--json"], timeout=60)
    global _NB_CACHE
    _NB_CACHE = None
    # Normalize: nlm returns notebook_id; expose as id for frontend compatibility
    if isinstance(data, dict) and "notebook_id" in data and "id" not in data:
        data = {**data, "id": data["notebook_id"]}
    return data


@router.post("/api/nblm/notebooks/{nid}/delete")
async def delete_notebook(nid: str, body: DeleteBody) -> dict:
    actual = next((n.get("title") for n in await _cached_notebooks() if n.get("id") == nid), None)
    if actual is None:  # cache miss → refresh once
        actual = next((n.get("title") for n in await _cached_notebooks(refresh=True)
                       if n.get("id") == nid), None)
    if actual is None:
        raise HTTPException(404, "notebook not found")
    if body.confirm_title != actual:
        raise HTTPException(400, "confirm_title does not match the notebook title")
    # nlm notebook delete nid -y (positional nid, not -n flag)
    await _run_cli([CLI, "notebook", "delete", nid, "-y"], timeout=60, parse=False)
    global _NB_CACHE
    _NB_CACHE = None
    return {"deleted": nid}


@router.get("/api/nblm/notebooks/{nid}/sources")
async def sources(nid: str) -> dict:
    # nlm source list nid --json → list directly
    data = await _run_cli([CLI, "source", "list", nid, "--json"], timeout=30)
    srcs = data if isinstance(data, list) else data.get("sources", []) if isinstance(data, dict) else []
    return {"sources": list(srcs or [])}


@router.post("/api/nblm/notebooks/{nid}/sources")
async def add_source(nid: str, body: SourceBody) -> dict:
    if body.url:
        target = body.url
    elif body.path:
        p = Path(body.path).expanduser().resolve()
        if not _under(p, [_browse_root()]):
            raise HTTPException(400, "path outside browse_root")
        target = str(p)
    else:
        raise HTTPException(400, "provide a url or path")
    # label keeps the tail — for file paths that's the filename, the useful part
    return _spawn("source", f"source: {target[-40:]}",
                  lambda j: _flow_source(j, nid, target))


@router.post("/api/nblm/notebooks/{nid}/research")
async def research(nid: str, body: ResearchBody) -> dict:
    if body.mode not in ("fast", "deep"):
        raise HTTPException(400, "mode must be 'fast' or 'deep'")
    return _spawn("research", f"research: {body.query[:40]}",
                  lambda j: _flow_research(j, nid, body.query, body.mode))


@router.post("/api/nblm/notebooks/{nid}/ask")
async def ask(nid: str, body: AskBody) -> dict:
    # nlm notebook query nid question --json [-c conversation_id]
    argv = [CLI, "notebook", "query", nid, body.question, "--json"]
    if body.conversation_id:
        argv += ["-c", body.conversation_id]
    return await _run_cli(argv, timeout=120)


@router.get("/api/nblm/catalog")
def catalog() -> dict:
    return {"types": _CATALOG}


def _validate_generate(ntype: str, options: dict, instructions: str | None) -> tuple[dict, list[str]]:
    cat = _CATALOG_BY_ID.get(ntype)
    if not cat:
        raise HTTPException(400, f"unknown type {ntype!r}")
    if cat["needs_instructions"] and not instructions:
        raise HTTPException(400, f"{ntype} requires instructions")
    valid = {g["flag"]: set(g["values"]) for g in cat["groups"]}
    opt_args: list[str] = []
    for flag, value in (options or {}).items():
        if flag not in valid:
            raise HTTPException(400, f"unknown option {flag!r} for {ntype}")
        if value not in valid[flag]:
            raise HTTPException(400, f"{value!r} is not a valid {flag}")
        opt_args += [flag, value]
    return cat, opt_args


@router.post("/api/nblm/notebooks/{nid}/generate")
async def generate(nid: str, body: GenerateBody) -> dict:
    if any(j.kind == "generate" and j.status in _RUNNING for j in _JOBS.values()):
        raise HTTPException(409, "a generation is already queued/running")
    cat, opt_args = _validate_generate(body.type, body.options, body.instructions)
    title = await _notebook_title(nid)
    return _spawn("generate", f"{body.type}: {title[:30]}",
                  lambda j: _flow_generate(j, nid, body.type, opt_args, body.instructions,
                                           title, cat["ext"]))


@router.get("/api/nblm/notebooks/{nid}/artifacts")
async def artifacts(nid: str) -> dict:
    # nlm studio status nid --json → list of artifacts
    data = await _run_cli([CLI, "studio", "status", nid, "--json"], timeout=30)
    arts = data if isinstance(data, list) else data.get("artifacts", []) if isinstance(data, dict) else []
    return {"artifacts": list(arts or [])}


@router.post("/api/nblm/artifacts/download")
async def download_artifact(body: DownloadBody) -> dict:
    cat = _CATALOG_BY_ID.get(body.type)
    if not cat:
        raise HTTPException(400, f"unknown type {body.type!r}")
    title = await _notebook_title(body.notebook_id)
    return _spawn("download", f"download {body.type}",
                  lambda j: _flow_download(j, body.notebook_id, body.type, body.artifact_id,
                                           title, cat["ext"]))


_OUT_EXTS = {".mp3", ".mp4", ".png", ".pdf", ".md", ".json", ".csv", ".pptx", ".html"}


@router.get("/api/nblm/outputs")
def outputs(notebook: str | None = None) -> dict:
    d = _output_dir()
    items: list[tuple[float, Path]] = []
    if d.is_dir():
        roots = [(d / notebook).resolve()] if notebook else None
        for p in d.rglob("*"):
            if not (p.is_file() and p.suffix.lower() in _OUT_EXTS):
                continue
            if roots and not _under(p, roots):
                continue
            items.append((p.stat().st_mtime, p))
    items.sort(reverse=True)
    return {"files": [{"name": p.name, "path": str(p), "mtime": int(mt),
                       "ext": p.suffix.lower().lstrip(".")}
                      for mt, p in items]}


@router.get("/api/nblm/outputs/file")
def outputs_file(path: str) -> FileResponse:
    root = _output_dir()
    p = Path(path).expanduser().resolve()
    if not _under(p, [root]):
        raise HTTPException(403, "path outside output_dir")
    if not p.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(p)


@router.post("/api/nblm/polish")
async def polish(body: PolishBody) -> dict:
    if body.purpose not in ("chat", "generate"):
        raise HTTPException(400, "purpose must be 'chat' or 'generate'")
    kind = "chat question" if body.purpose == "chat" else "generation instruction"
    msg = (f"Rewrite the following draft into a clear, specific NotebookLM {kind}. "
           f"Keep the same language as the draft. Return ONLY the rewritten text, "
           f"no preamble or quotation.\n\nDraft:\n{body.draft}")
    return {"polished": await zai_message(msg)}


@router.get("/api/nblm/jobs")
def jobs() -> dict:
    return {"jobs": [j.to_dict() for j in reversed(list(_JOBS.values()))]}


@router.post("/api/nblm/jobs/{jid}/cancel")
async def cancel_job(jid: str) -> dict:
    j = _JOBS.get(jid)
    if not j:
        raise HTTPException(404, "unknown job")
    if j.status not in _RUNNING:
        raise HTTPException(409, f"job is {j.status}, nothing to cancel")
    j.cancel = True
    if j.proc is not None:
        j.proc.send_signal(signal.SIGTERM)
    return {"status": "cancelling"}
