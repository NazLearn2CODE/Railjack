"""ffmpeg "Video Lab" — op builders + async job runner + REST endpoints.

One op at a time (``asyncio.Lock`` + a 409 if any job is non-terminal). Every
client-supplied path is resolved and confined under a configured root
(``media_dirs`` for clips, ``lut_dir`` for LUTs) via ``os.path.commonpath``.
Outputs go only to ``output_dir`` (created on demand), timestamped.

Op builders return argv **lists** (never shell strings) — run with
``asyncio.create_subprocess_exec`` (never ``shell=True``, never sudo). They
follow ``skills/f5-ffmpeg-video/references/recipes.md``; the recipe section is
cited in a comment per builder. Normalize (§0) is built into the multi-input
ops (concat/xfade) where it is required, not a user-facing knob.

Progress comes from ffmpeg ``-progress pipe:1 -nostats``: stdout is parsed for
``out_time_us`` (microseconds, despite the legacy ``out_time_ms`` alias) and
divided by the total output duration (ffprobe'd; xfade subtracts overlaps,
concat sums the parts). UI polls 1 s — no websockets.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import CONFIG

router = APIRouter()

# ---------------------------------------------------------------- config roots

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".ts"}
LUT_EXTS = {".cube", ".3dl", ".dat", ".csp", ".mga"}


def _ffmpeg_module_options() -> dict:
    for m in CONFIG.modules:
        if m.kind == "panel" and m.panel == "ffmpeg":
            return m.options or {}
    return {}


_OPTS = _ffmpeg_module_options()


def _media_roots() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in _OPTS.get("media_dirs", [])]


def _lut_root() -> Path:
    return Path(_OPTS["lut_dir"]).expanduser().resolve()


def _output_root() -> Path:
    o = Path(_OPTS["output_dir"]).expanduser().resolve()
    o.mkdir(parents=True, exist_ok=True)
    return o


# ---------------------------------------------------------------- path safety


def _under(path: Path, roots: list[Path]) -> bool:
    """True if ``path`` (already resolved) lives at or under one of ``roots``."""
    for root in roots:
        try:
            if os.path.commonpath([str(path), str(root)]) == str(root):
                return True
        except ValueError:
            # different drives (not on linux) → definitely not under
            continue
    return False


def _safe_input(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    if not _under(p, _media_roots()):
        raise ValueError(f"path not under a configured media dir: {raw}")
    return p


def _safe_lut(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    if not _under(p, [_lut_root()]):
        raise ValueError(f"LUT not under configured lut_dir: {raw}")
    return p


# ---------------------------------------------------------------- ffprobe


def _probe_one(p: Path) -> tuple[float, bool]:
    """Return (duration_s, has_audio) via ffprobe. Raises on failure."""
    argv = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(p),
    ]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {p.name}: {r.stderr.strip()}")
    data = json.loads(r.stdout or "{}")
    dur = float(data.get("format", {}).get("duration") or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return dur, has_audio


# ---------------------------------------------------------------- op builders
# recipes.md sections (skill f5-ffmpeg-video): §M master block, §0 normalize,
# §1 concat, §2 xfade offset formula, §3 lut3d, §8 DNxHR.

FPS = "30000/1001"   # 29.97 — master timeline fps for §0 normalize
SIZE = "1920:1080"


def _master() -> list[str]:
    """§M master defaults block: x264 medium CRF 18, yuv420p, AAC 192k, +faststart."""
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]


def _vnorm(i: int) -> str:
    """§0 normalize one video stream: scale+letterbox, SAR 1, fps, yuv420p."""
    return (f"[{i}:v]scale={SIZE}:force_original_aspect_ratio=decrease,"
            f"pad={SIZE}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[v{i}]")


def _anorm(i: int) -> str:
    """§0 normalize one audio stream: 48k, fltp, stereo so concat/xfade segments match.

    ``aformat=channel_layouts=stereo`` lets ffmpeg's auto-inserted swresample
    upmix/downmix correctly (mono→stereo, 5.1→stereo) — a static ``pan`` can't
    cover both without dropping a channel.
    """
    return f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"


def transcode_h264(inputs: list[Path], out: Path, _p: dict) -> list[str]:
    """§M: transcode a single clip to the H.264 master format (no rescale)."""
    return ["ffmpeg", "-y", "-i", str(inputs[0]), *_master(), str(out)]


def transcode_dnxhr(inputs: list[Path], out: Path, _p: dict) -> list[str]:
    """§8: transcode a single clip to DNxHR SQ (Resolve-edit intermediate, .mov)."""
    return ["ffmpeg", "-y", "-i", str(inputs[0]),
            "-c:v", "dnxhd", "-profile:v", "dnxhr_sq", "-pix_fmt", "yuv422p",
            "-r", FPS, "-c:a", "pcm_s16le", str(out)]


def lut(inputs: list[Path], out: Path, p: dict) -> list[str]:
    """§3: apply a 3D LUT to a single clip, then encode with the master block."""
    # lut3d path is one argv token (no shell) — fine while lut_dir has no
    # spaces/colons; escape filtergraph-style if that ever changes.
    return ["ffmpeg", "-y", "-i", str(inputs[0]),
            "-vf", f"lut3d={p['lut']}", *_master(), str(out)]


def concat(inputs: list[Path], out: Path, p: dict) -> list[str]:
    """§0 + §1: normalize each input, then concat filter (re-encode bakes §0 in)."""
    audio = p["audio"]
    chains: list[str] = []
    cat_in: list[str] = []
    for i in range(len(inputs)):
        chains.append(_vnorm(i))
        if audio:
            chains.append(_anorm(i))
            cat_in.append(f"[v{i}][a{i}]")
        else:
            cat_in.append(f"[v{i}]")
    a = "1" if audio else "0"
    label = "[vout][aout]" if audio else "[vout]"
    filt = ";".join(chains) + ";" + "".join(cat_in) + f"concat=n={len(inputs)}:v=1:a={a}{label}"
    maps = ["-map", "[vout]"] + (["-map", "[aout]"] if audio else [])
    argv = ["ffmpeg", "-y"]
    for q in inputs:
        argv += ["-i", str(q)]
    argv += ["-filter_complex", filt, *maps, *_master(), str(out)]
    return argv


def xfade(inputs: list[Path], out: Path, p: dict) -> list[str]:
    """§0 + §2: normalize each input, then chain xfade (offset_k = Σdur[0..k] − (k+1)·d).

    §2 offset formula: the k-th crossfade (into clip k+1) starts at the cumulative
    duration of clips 0..k minus the (k+1) overlaps already consumed.
    """
    d = p["transition"]
    dur = p["durations"]
    audio = p["audio"]
    n = len(inputs)

    chains: list[str] = []
    for i in range(n):
        chains.append(_vnorm(i))
        if audio:
            chains.append(_anorm(i))

    offsets: list[float] = []
    cum = 0.0
    for k in range(n - 1):
        cum += dur[k]
        offsets.append(cum - (k + 1) * d)

    prev = "v0"
    for k in range(n - 1):
        nxt = f"vx{k + 1}" if k < n - 2 else "vout"
        chains.append(
            f"[{prev}][v{k + 1}]xfade=transition=fade:duration={d}:"
            f"offset={offsets[k]:.3f}[{nxt}]"
        )
        prev = nxt

    maps = ["-map", "[vout]"]
    if audio:
        aprev = "a0"
        for k in range(n - 1):
            anxt = f"ax{k + 1}" if k < n - 2 else "aout"
            chains.append(f"[{aprev}][a{k + 1}]acrossfade=d={d}[{anxt}]")
            aprev = anxt
        maps = ["-map", "[vout]", "-map", "[aout]"]

    argv = ["ffmpeg", "-y"]
    for q in inputs:
        argv += ["-i", str(q)]
    argv += ["-filter_complex", ";".join(chains), *maps, *_master(), str(out)]
    return argv


BUILDERS = {
    "transcode_h264": transcode_h264,
    "concat": concat,
    "lut": lut,
    "xfade": xfade,
    "transcode_dnxhr": transcode_dnxhr,
}

# ops that take exactly one clip vs. ops that need >= 2 (ordered)
_MULTI = {"concat", "xfade"}


def _total_duration(op: str, probes: list[tuple[float, bool]], p: dict) -> float:
    durs = [d for d, _ in probes]
    if op == "xfade":
        # §2: total = Σdur − (n−1)·transition (each crossfade overlaps the join)
        return max(0.0, sum(durs) - (len(durs) - 1) * p["transition"])
    return sum(durs)  # concat = Σdur; single-clip ops = that clip's duration


# ---------------------------------------------------------------- job state

@dataclass
class Job:
    id: str
    op: str
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: int = 0
    logs: deque = field(default_factory=lambda: deque(maxlen=200))
    output_path: str | None = None
    error: str | None = None
    proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    cancel: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "op": self.op,
            "status": self.status,
            "progress": self.progress,
            "output_path": self.output_path,
            "error": self.error,
            "logs": list(self.logs),
        }


_JOBS: dict[str, Job] = {}
_LOCK = asyncio.Lock()  # serializes runners; the 409 check already prevents overlap
_RUNNING = {"queued", "running"}
# Hold refs to job tasks so CPython doesn't GC them mid-run (mirrors manage._BG).
_BG: set[asyncio.Task[object]] = set()


async def _execute(job: Job, argv: list[str], total_us: int) -> None:
    async with _LOCK:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            job.status, job.error = "error", "ffmpeg binary not found on PATH"
            return

        job.proc = proc
        job.status = "running"

        async def pump_progress() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                s = line.decode(errors="replace").strip()
                # out_time_us and the legacy out_time_ms alias are BOTH microseconds
                if s.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        us = int(s.split("=", 1)[1])
                    except ValueError:
                        continue
                    if total_us > 0:
                        job.progress = max(0, min(99, int(us / total_us * 100)))

        async def pump_logs() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                job.logs.append(line.decode(errors="replace").rstrip())

        await asyncio.gather(pump_progress(), pump_logs())
        rc = await proc.wait()
        job.proc = None

        if job.cancel or rc in (-signal.SIGTERM, -signal.SIGINT):
            job.status = "cancelled"
        elif rc == 0:
            job.status, job.progress = "done", 100
        else:
            job.status, job.error = "error", f"ffmpeg exited {rc}"


# ---------------------------------------------------------------- output naming


def _output_path(op: str) -> Path:
    ext = ".mov" if op == "transcode_dnxhr" else ".mp4"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return _output_root() / f"{op}_{ts}{ext}"


# ---------------------------------------------------------------- API models


class JobBody(BaseModel):
    op: str
    files: list[str]
    lut: str | None = None
    transition: float | None = None


def _ensure_configured() -> None:
    if not _OPTS.get("media_dirs") or not _OPTS.get("output_dir"):
        raise HTTPException(503, "ffmpeg module has no media_dirs/output_dir configured")


# ---------------------------------------------------------------- endpoints


@router.get("/api/ffmpeg/files")
def files() -> dict:
    """Video files across all media_dirs, tagged by root. Missing dirs skip silently."""
    out: list[dict] = []
    for root in _media_roots():
        if not root.is_dir():
            continue  # e.g. ~/Downloads/B-Rolls absent — never 500
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                out.append({"root": root.name, "name": p.name, "path": str(p)})
    return {"files": out}


@router.get("/api/ffmpeg/luts")
def luts() -> dict:
    out: list[dict] = []
    try:
        root = _lut_root()
    except KeyError:
        return {"luts": []}
    if root.is_dir():
        for p in sorted(root.glob("*")):
            if p.is_file() and p.suffix.lower() in LUT_EXTS:
                out.append({"name": p.name, "path": str(p)})
    return {"luts": out}


def _validate(op: str, body: JobBody) -> tuple[list[Path], Path, dict]:
    """Resolve + confine paths, check op arity, collect op params. No ffprobe."""
    _ensure_configured()
    if op not in BUILDERS:
        raise HTTPException(400, f"unknown op {op!r}; want one of {sorted(BUILDERS)}")

    try:
        inputs = [_safe_input(p) for p in body.files]
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not inputs:
        raise HTTPException(400, "select at least one file")

    if op in _MULTI:
        if len(inputs) < 2:
            raise HTTPException(400, f"{op} needs at least 2 files (ordered)")
    elif len(inputs) != 1:
        raise HTTPException(400, f"{op} takes exactly one file")

    params: dict = {}
    if op == "lut":
        if not body.lut:
            raise HTTPException(400, "lut op needs a LUT")
        try:
            params["lut"] = _safe_lut(body.lut)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if op == "xfade":
        params["transition"] = body.transition if body.transition and body.transition > 0 else 0.5
    return inputs, _output_path(op), params


@router.post("/api/ffmpeg/jobs")
async def create_job(body: JobBody) -> dict:
    op = body.op

    # one at a time: 409 if anything is still queued/running
    if any(j.status in _RUNNING for j in _JOBS.values()):
        raise HTTPException(409, "a job is already running")

    inputs, out, params = _validate(op, body)

    # ffprobe off the event loop (blocking, but a couple clips — not a hot path)
    try:
        probes = await asyncio.to_thread(lambda: [_probe_one(p) for p in inputs])
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise HTTPException(400, str(e))

    params["audio"] = all(has_a for _, has_a in probes)
    params["durations"] = [d for d, _ in probes]

    argv = BUILDERS[op](inputs, out, params)
    # global flags the progress pump depends on: machine-readable key=value
    # progress on stdout, human stats (which would corrupt it) off
    argv[1:1] = ["-progress", "pipe:1", "-nostats"]
    total_us = int(_total_duration(op, probes, params) * 1_000_000)

    jid = uuid4().hex[:8]
    job = Job(id=jid, op=op, output_path=str(out))
    _JOBS[jid] = job
    task = asyncio.create_task(_execute(job, argv, total_us))
    _BG.add(task)
    task.add_done_callback(_BG.discard)
    return {"id": jid}


@router.get("/api/ffmpeg/jobs")
def list_jobs() -> dict:
    # newest first so the active job sits on top
    return {"jobs": [j.to_dict() for j in reversed(list(_JOBS.values()))]}


@router.get("/api/ffmpeg/jobs/{jid}")
def get_job(jid: str) -> dict:
    j = _JOBS.get(jid)
    if not j:
        raise HTTPException(404, "unknown job")
    return j.to_dict()


@router.post("/api/ffmpeg/jobs/{jid}/cancel")
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
