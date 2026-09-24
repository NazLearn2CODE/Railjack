"""CODEATLAS module — named-target architecture maps (Naz + Damriw dispatch,
2026-09-24; grill round settled: agy lane, folder + feature-focus targets,
maps live in <target>/CODEATLAS/).

Name a target — a Coding Project repo, a skill folder, or Railjack itself
(optionally focused on one feature/flow, e.g. "IDE SCOUT") — and the module
drives the Antigravity CLI (agy) with the CodeAtlas methodology brief. agy
reads the target directly and writes a single-file interactive architecture
map INTO ``<target>/CODEATLAS/`` (codeatlas.html + module-map.json +
summary.md). The MODULE stamps ``meta.json`` (mapped HEAD sha + timestamp) —
staleness = target HEAD moved since the map. Single-file law (no fetch(), no
external script/link URLs, ``id="codeatlas-data"`` present) is validated here
after every run; a failing run reports errors instead of serving a map.

Jev house rule: artifact SHAPE is verified here against the CodeAtlas
contract; map CONTENT claims stay Naz's eyeball + the summary's own
blind-spots section (the module never silently trusts the model's map).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import CONFIG

router = APIRouter()

_REPO_ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------------ options


def _opts() -> dict:
    """This module's options block, read fresh each call (reload-safe)."""
    for m in CONFIG.modules:
        if m.id == "codeatlas":
            return m.options
    return {}


def _projects_root() -> Path:
    return Path(_opts().get("projects_root", "~/Coding Projects")).expanduser()


def _skill_roots() -> list[Path]:
    roots = _opts().get("skill_roots") or ["~/skill-library/skills", "~/.dsh/skills"]
    return [Path(r).expanduser() for r in roots]


def _atlas_repo() -> Path:
    """The CodeAtlas skill checkout (template source)."""
    return Path(_opts().get("codeatlas_repo", "~/Coding Projects/CodeAtlas")).expanduser()


def _agy_timeout() -> int:
    return int(_opts().get("agy_timeout", 900))


# ------------------------------------------------------------- target resolve


def resolve_target(name: str) -> dict:
    """Resolve a user-typed name to a mappable folder.

    Order: Railjack (its modules map the whole repo) → Coding Projects →
    skill folders. Raises 400 with the tried paths when nothing matches."""
    name = (name or "").strip().strip("/")
    if not name:
        raise HTTPException(400, "target name is empty")
    tried: list[str] = []
    if name.lower() in ("railjack", "railjack repo"):
        return {"path": _REPO_ROOT, "kind": "repo", "label": "Railjack"}
    tried.append(str(_REPO_ROOT))
    app_mod = _REPO_ROOT / "app" / f"{name}.py"
    if app_mod.exists():  # a Railjack module name maps the whole repo, focused
        return {"path": _REPO_ROOT, "kind": "repo", "label": f"Railjack · {name}"}
    proj = _projects_root() / name
    tried.append(str(proj))
    if proj.is_dir():
        return {"path": proj, "kind": "repo" if (proj / ".git").exists() else "folder",
                "label": name}
    for root in _skill_roots():
        skill = root / name
        tried.append(str(skill))
        if skill.is_dir():
            return {"path": skill, "kind": "skill", "label": f"skill · {name}"}
    raise HTTPException(400, f"no mappable target named '{name}'. Tried: " + ", ".join(tried))


def _head_sha(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    try:
        import subprocess
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip() or None
    except Exception:
        return None


def map_dir(target_path: Path) -> Path:
    return target_path / "CODEATLAS"


def read_meta(target_path: Path) -> dict | None:
    try:
        meta = json.loads((map_dir(target_path) / "meta.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else None
    except Exception:
        return None


def check_target(name: str) -> dict:
    """Presence + staleness for a target's map: missing | current | stale."""
    t = resolve_target(name)
    meta = read_meta(t["path"])
    if not meta:
        return {"target": t["label"], "state": "missing", "map_url": None}
    head = _head_sha(t["path"])
    mapped = meta.get("head")
    if t["kind"] == "repo" and head and mapped and head != mapped:
        state = "stale"
    else:
        state = "current"
    age_days = round((time.time() - float(meta.get("generated", 0))) / 86400, 1)
    return {"target": t["label"], "state": state, "mapped_head": mapped,
            "head": head, "age_days": age_days, "kind": t["kind"],
            "focus": meta.get("focus") or ""}


# ----------------------------------------------------------- artifact law


def validate_artifacts(mapdir: Path) -> list[str]:
    """CodeAtlas contract checks. Empty list = valid. Shape only — content
    claims stay Naz's eyeball (Jev house rule)."""
    errors: list[str] = []
    html_p = mapdir / "codeatlas.html"
    json_p = mapdir / "module-map.json"
    if not html_p.exists():
        errors.append("codeatlas.html missing")
    if not json_p.exists():
        errors.append("module-map.json missing")
    if errors:
        return errors
    try:
        html = html_p.read_text(encoding="utf-8")
    except Exception as e:
        return [f"html unreadable: {e}"]
    if 'id="codeatlas-data"' not in html:
        errors.append("html missing id=\"codeatlas-data\" payload")
    if "fetch(" in html:
        errors.append("html contains fetch( — single-file law broken")
    if re.search(r'<script[^>]+src="https?://', html) or re.search(r'<link[^>]+href="https?://', html):
        errors.append("html references external script/link URLs — single-file law broken")
    try:
        mm = json.loads(json_p.read_text(encoding="utf-8"))
        if not isinstance(mm.get("modules"), list) or not mm["modules"]:
            errors.append("module-map.json has no modules")
        if not isinstance(mm.get("relations"), list):
            errors.append("module-map.json has no relations list")
    except Exception as e:
        errors.append(f"module-map.json unparseable: {e}")
    return errors


def _gitignore_law(target: Path) -> str | None:
    """Generated maps never dirty the target repo: CODEATLAS/ must be
    ignored. Appends when missing; None on repos without git needs."""
    if not (target / ".git").exists():
        return None
    gi = target / ".gitignore"
    try:
        if gi.exists():
            if any(line.strip() in ("CODEATLAS/", "/CODEATLAS/")
                   for line in gi.read_text(encoding="utf-8").splitlines()):
                return None
            with gi.open("a", encoding="utf-8") as f:
                f.write("\nCODEATLAS/\n")
            return "appended"
        gi.write_text("CODEATLAS/\n", encoding="utf-8")
        return "created"
    except Exception as e:  # noqa: BLE001 — cosmetic law, never kills a run
        return f"failed: {e}"


# ------------------------------------------------------------------- jobs


@dataclass
class AtlasJob:
    id: str
    kind: str
    label: str
    target_path: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: int = 0
    logs: deque = field(default_factory=lambda: deque(maxlen=200))
    error: str | None = None
    cancel: bool = False
    result: dict | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "status": self.status, "progress": self.progress,
                "logs": list(self.logs), "error": self.error,
                "target_path": self.target_path, "result": self.result}


_JOBS: dict[str, AtlasJob] = {}
_BG: set[asyncio.Task[object]] = set()


class _Cancelled(Exception):
    pass


async def _run_job(job: AtlasJob, flow) -> None:
    job.status = "running"
    try:
        await flow
    except _Cancelled:
        job.status = "cancelled"
    except Exception as e:  # noqa: BLE001 — any failure → error status
        job.status, job.error = "error", str(e) or e.__class__.__name__
    else:
        if not job.cancel:
            job.status, job.progress = "done", 100


def _spawn(kind: str, label: str, target_path: Path, flow) -> dict:
    jid = uuid4().hex[:8]
    job = AtlasJob(id=jid, kind=kind, label=label, target_path=str(target_path))
    _JOBS[jid] = job
    task = asyncio.create_task(_run_job(job, flow(job)))
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return {"id": jid}


# ------------------------------------------------------------------- brief


def _brief(target: dict, focus: str) -> str:
    template = _atlas_repo() / "assets" / "codeatlas-single-file-template.html"
    focus_block = ""
    if focus:
        focus_block = (
            f"\n## FOCUS (Naz named this flow: “{focus}”)\n"
            f"Center the map on this feature/flow: trace its code path (entry → "
            f"routes → backend → scripts/output), mark the involved modules as the "
            f"core layer, and say in the summary how the flow reads/writes state.\n")
    return f"""You are producing a CodeAtlas architecture map for: {target['path']}

Follow the CodeAtlas skill methodology exactly (single-file interactive HTML
architecture map). Read these first:
- {template}  (copy this as your base — replace ONLY the JSON payload)
- {_atlas_repo()}/SKILL.md  (the full contract)

OUTPUT — write EXACTLY these files (create the directory if needed):
1. {target['path']}/CODEATLAS/module-map.json — {{"project", "source", "modules": [{{name, role}}], "relations": [{{source, target, type, reason}}], "entrypoints"}}. Architecture-level granularity (module-to-module), NOT function-level. Every relation's `reason` cites the concrete import/call/route that proves it.
2. {target['path']}/CODEATLAS/codeatlas.html — SINGLE-FILE, self-contained: copy the template, embed the map JSON inside <script id="codeatlas-data" type="application/json">…</script>. HARD LAWS: NO fetch( anywhere; NO external <script src="http…"> or <link href="http…">; must render project summary, module cards, relation table, layered SVG relationship graph with direction arrows + legend, and the framework flow graph below it.
3. {target['path']}/CODEATLAS/summary.md — architecture conclusion, top relationships (5-12 bullets), blind spots/uncertainty, file paths.
{focus_block}
RULES:
- Inspect folder structure, package manifests, and import/use patterns yourself before writing anything.
- Do NOT invent modules or relations you did not see evidence for.
- Do NOT write any meta.json — the calling module owns that file.
- Keep everything else about the template untouched (styles, layout, render JS).
- Begin the summary.md with a 5-10 line architecture conclusion.
"""


# ------------------------------------------------------------------- routes


class GenerateReq(BaseModel):
    target: str
    focus: str = ""


@router.post("/api/codeatlas/generate")
async def generate(req: GenerateReq) -> dict:
    """Map a named target: spawn the agy run, poll /job/{id}. Single-flight
    per target — one map run at a time (agy quota + no double-writes)."""
    t = resolve_target(req.target)
    if any(j.status in ("queued", "running") and j.target_path == str(t["path"])
           for j in _JOBS.values()):
        raise HTTPException(409, f"a CODEATLAS run for {t['label']} is already running")
    if not shutil.which("agy"):
        raise HTTPException(503, "agy CLI not on PATH — cannot run the atlas lane")
    mapdir = map_dir(t["path"])
    head = _head_sha(t["path"])
    focus = (req.focus or "").strip()

    async def flow(job: AtlasJob) -> None:
        job.logs.append("brief built; launching agy…")
        argv = ["agy", "-p", _brief(t, focus),
                "--add-dir", str(t["path"]),
                "--effort", "medium",
                "--dangerously-skip-permissions",
                "--print-timeout", "12m"]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(t["path"]))
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_agy_timeout())
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"agy timed out after {_agy_timeout()}s")
        job.progress = 70
        if proc.returncode != 0:
            raise RuntimeError(f"agy exit {proc.returncode}: {err.decode(errors='replace')[:300]}")
        job.logs.append("agy done; validating artifacts…")
        errors = validate_artifacts(mapdir)
        if errors:
            raise RuntimeError("artifact law violated: " + "; ".join(errors))
        (mapdir / "meta.json").write_text(json.dumps({
            "generated": time.time(), "head": head, "target": t["label"],
            "kind": t["kind"], "focus": focus, "generator": "agy",
        }, indent=2), encoding="utf-8")
        job.logs.append(f"gitignore: {_gitignore_law(t['path']) or 'already ignored'}")
        job.result = {"validated": True, "head": head, "focus": focus,
                      "mapdir": str(mapdir),
                      "map_url": f"/api/codeatlas/map/{job.id}"}

    return _spawn("codeatlas", f"CODEATLAS · {t['label']}" + (f" · {focus}" if focus else ""),
                  t["path"], flow)


@router.get("/api/codeatlas/job/{jid}")
async def job_status(jid: str) -> dict:
    job = _JOBS.get(jid)
    if not job:
        raise HTTPException(404, "no such CODEATLAS job")
    return job.to_dict()


@router.get("/api/codeatlas/map/{jid}")
async def serve_map(jid: str):
    """Serve the generated single-file map (iframe / new-tab target)."""
    job = _JOBS.get(jid)
    if not job or job.status != "done":
        raise HTTPException(404, "no finished CODEATLAS job with that id")
    html_p = Path(job.target_path) / "CODEATLAS" / "codeatlas.html"
    if not html_p.exists():
        raise HTTPException(404, "map file vanished from the target folder")
    return HTMLResponse(html_p.read_text(encoding="utf-8"))


@router.get("/api/codeatlas/check")
async def check(target: str) -> dict:
    """Presence + staleness for a target's map (CURRENT / STALE / missing)."""
    return check_target(target)


@router.get("/api/codeatlas/open-map")
async def open_map(target: str):
    """Serve the CURRENT map straight from the target folder (no job needed)."""
    t = resolve_target(target)
    html_p = map_dir(t["path"]) / "codeatlas.html"
    if not html_p.exists():
        raise HTTPException(404, f"no map in {map_dir(t['path'])} — run GENERATE first")
    return HTMLResponse(html_p.read_text(encoding="utf-8"))
