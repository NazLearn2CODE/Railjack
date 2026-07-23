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

ANALYZE ops (``ANALYZERS``) are read-only: one ff* run parsed in Python, a
JSON-able ``result`` + ``<op>_<ts>.json`` sidecar (no rendered file, no
incremental progress).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
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


# ---------------------------------------------------------------- xfade transitions (live)
# The TRANSITION dropdown renders this verbatim, so the list auto-updates with
# ffmpeg upgrades — never hardcode transition names. Mirrors caps.sh's awk: the
# xfade help lists each style as a bare word followed by its default int value.
_XFADE: list[str] | None = None
_TRANS_LINE = re.compile(r"^\s+([a-z][a-z0-9_]*)\s+-?\d+\s")


def _xfade_transitions() -> list[str]:
    """Sorted xfade transition names this ffmpeg build supports (cached at first call)."""
    global _XFADE
    if _XFADE is not None:
        return _XFADE
    found: set[str] = set()
    try:
        r = subprocess.run(
            ["ffmpeg", "-h", "filter=xfade"], capture_output=True, text=True, timeout=10
        )
        for ln in (r.stdout or "").splitlines():
            m = _TRANS_LINE.match(ln)
            if m:
                found.add(m.group(1))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    names = sorted(found)
    if "fade" not in names:
        names = ["fade", *names]  # safe fallback so xfade always has a default
    _XFADE = names
    return names


def _media_roots() -> list[Path]:
    return [Path(p).expanduser().resolve() for p in _OPTS.get("media_dirs", [])]


def _lut_root() -> Path:
    return Path(_OPTS["lut_dir"]).expanduser().resolve()


def _browse_root() -> Path:
    """Root the file browser + job inputs/outputs are confined under (default ~)."""
    return Path(_OPTS.get("browse_root", "~")).expanduser().resolve()


def _default_output_dir() -> Path:
    """Where renders land unless the UI picks another folder.

    YAML ``output_dir`` sets it; absent, ``~/Downloads/VDO Outputs``.
    """
    o = _OPTS.get("output_dir")
    if o:
        return Path(o).expanduser().resolve()
    return (Path.home() / "Downloads" / "VDO Outputs").resolve()


def _input_roots() -> list[Path]:
    """Confinement roots for job inputs: configured ``media_dirs`` ∪ browse root.

    ``media_dirs`` keeps existing configs (and the test fixture's tmp root)
    valid; the browse root lets footage come from anywhere under ~ that the
    user navigated to via the ADD FOOTAGE browser.
    """
    roots = _media_roots()
    br = _browse_root()
    if br not in roots:
        roots.append(br)
    return roots


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
    if not _under(p, _input_roots()):
        raise ValueError(f"path not under a browsable media dir: {raw}")
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
            f"[{prev}][v{k + 1}]xfade=transition={p.get('style', 'fade')}:duration={d}:"
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


# ---------------------------------------------------------------- single-clip effects (§4–§6 family)
# One clip in, one clip out; each maps to a recipes.md section.

def fade(inputs: list[Path], out: Path, p: dict) -> list[str]:
    """§5: fade video in at 0 and out at DUR−fd (audio too, if present)."""
    fd = p["fade"]
    st_out = max(0.0, p["durations"][0] - fd)
    vf = f"fade=t=in:st=0:d={fd},fade=t=out:st={st_out}:d={fd}"
    argv = ["ffmpeg", "-y", "-i", str(inputs[0]), "-vf", vf]
    if p["audio"]:
        argv += ["-af", f"afade=t=in:st=0:d={fd},afade=t=out:st={st_out}:d={fd}"]
    return argv + _master() + [str(out)]


def _atempo_chain(factor: float) -> str:
    """atempo spans [0.5, 2.0] per stage; chain to reach factor in [0.25, 16].

    ponytail: ceiling is 16 (four 2× stages) — beyond that use a real editor.
    """
    parts: list[str] = []
    cur = factor
    while cur > 2.0:
        parts.append("atempo=2.0")
        cur /= 2.0
    while cur < 0.5:
        parts.append("atempo=0.5")
        cur /= 0.5
    parts.append(f"atempo={cur}")
    return ",".join(parts)


def speed(inputs: list[Path], out: Path, p: dict) -> list[str]:
    """§6: speed ramp. video setpts=1/factor*PTS; audio atempo chain."""
    f = p["speed"]
    vpts = 1.0 / f
    if p["audio"]:
        filt = f"[0:v]setpts={vpts}*PTS[v];[0:a]{_atempo_chain(f)}[a]"
        return ["ffmpeg", "-y", "-i", str(inputs[0]), "-filter_complex", filt,
                "-map", "[v]", "-map", "[a]", *_master(), str(out)]
    return ["ffmpeg", "-y", "-i", str(inputs[0]), "-vf", f"setpts={vpts}*PTS", *_master(), str(out)]


def _vf_op(filt: str):
    """Factory: a single-clip op that applies one -vf filter + the master block.
    The 7 taste effects (denoise/sharpen/…) are each one filter string — DRY."""
    def _build(inputs: list[Path], out: Path, _p: dict) -> list[str]:
        return ["ffmpeg", "-y", "-i", str(inputs[0]), "-vf", filt, *_master(), str(out)]
    return _build


denoise = _vf_op("hqdn3d=4:3:6:4.5")     # luma_spatial:chroma:luma_tmp:chroma_tmp
sharpen = _vf_op("unsharp=5:5:1.0:5:5:0.0")  # luma 5×5 amt 1.0, chroma off
vignette = _vf_op("vignette=PI/5")
grain = _vf_op("noise=alls=20:allf=t+u")  # allf=t+u = temporal+uniform (film grain)
mirrorh = _vf_op("hflip")
mirrorv = _vf_op("vflip")
grayscale = _vf_op("hue=s=0")             # saturation 0 = desaturate, keeps pixfmt

# 2026-07-20 batch — every filter probed against the installed ffmpeg 8.1.2
# (`ffmpeg -h filter=<name>`) before landing here.
# LOOK — color looks + the M6.4-deferred brightness/contrast/saturation trims
# (fixed steps: a parameterized version would need new FfmpegPanel inputs,
# breaking the zero-frontend-edit growth invariant).
sepia = _vf_op("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")
warm = _vf_op("colortemperature=temperature=4500")   # shift toward tungsten
cool = _vf_op("colortemperature=temperature=8500")   # shift toward shade/blue
vibrance = _vf_op("vibrance=intensity=0.4")          # boost muted colors, spare skin
brightness_up = _vf_op("eq=brightness=0.06")
brightness_down = _vf_op("eq=brightness=-0.06")
contrast_up = _vf_op("eq=contrast=1.15")
contrast_down = _vf_op("eq=contrast=0.9")
saturation_up = _vf_op("eq=saturation=1.3")
saturation_down = _vf_op("eq=saturation=0.7")
# EFFECTS — stylize + repair
negative = _vf_op("negate")
posterize = _vf_op("lutrgb=r=round(val/51)*51:g=round(val/51)*51:b=round(val/51)*51")  # 6 levels/channel
pixelize = _vf_op("pixelize=width=16:height=16")     # mosaic blocks
cartoon = _vf_op("edgedetect=mode=colormix:high=0")  # painted/comic mix
deband = _vf_op("deband")                            # smooth gradient banding
deflicker = _vf_op("deflicker")                      # even out luma flicker
sharpen_strong = _vf_op("unsharp=7:7:2.5:7:7:0.0")   # heavier 7×7 amt 2.5
denoise_heavy = _vf_op("hqdn3d=8:6:12:9")            # 2× the standard denoise


# 2026-07-23 — FRAME blur-fill. split/gblur/overlay probed against the
# installed ffmpeg 8.1.2 (`ffmpeg -h filter=<name>`) before landing, per the
# 2026-07-20 batch convention.
def fit_aspect(inputs: list[Path], out: Path, _p: dict) -> list[str]:
    """FRAME: blur-fill fit to the 16:9 bench — blurred cover background,
    aspect-fit foreground centered on top. Fixed constants (SIZE, sigma 20)
    per the zero-frontend-edit growth invariant."""
    filt = ("[0:v]split=2[bg][fg];"
            f"[bg]scale={SIZE}:force_original_aspect_ratio=increase,crop={SIZE},gblur=sigma=20[bg];"
            f"[fg]scale={SIZE}:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    return ["ffmpeg", "-y", "-i", str(inputs[0]), "-filter_complex", filt,
            "-map", "[v]", "-map", "0:a?", *_master(), str(out)]


def loudnorm(inputs: list[Path], out: Path, _p: dict) -> list[str]:
    """§7: EBU R128 loudness normalize. Audio filtered, video copied (no re-encode)."""
    return ["ffmpeg", "-y", "-i", str(inputs[0]), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]


def volume(inputs: list[Path], out: Path, p: dict) -> list[str]:
    """Audio gain (linear factor); video copied."""
    return ["ffmpeg", "-y", "-i", str(inputs[0]), "-af", f"volume={p['gain']}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]


# ------------------------------------------------------- analysis ops (ANALYZE)
# Read-only: no rendered output. One ff* subprocess, parsed in Python, returns
# a JSON-able dict. Stock documented filters — clean-room, no AGPL code.

def _lavfi_escape(path: str) -> str:
    """Escape a filename for movie= inside -f lavfi (2 parser levels:
    option parser \\ ' : then filtergraph parser \\ ' , ; [ ])."""
    l1 = re.sub(r"([\\':])", r"\\\1", path)
    return re.sub(r"([\\',;\[\]])", r"\\\1", l1)

def _scene_argv(inp: Path, threshold: float) -> list[str]:
    graph = f"movie={_lavfi_escape(str(inp))},select=gt(scene\\,{threshold})"
    return ["ffprobe", "-v", "quiet", "-show_entries", "frame=pts_time",
            "-of", "json", "-f", "lavfi", graph]

def _energy_argv(inp: Path) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-nostats", "-i", str(inp),
            "-vn", "-af", "ebur128", "-f", "null", "-"]

def _parse_scene_frames(stdout_json: str) -> list[float]:
    data = json.loads(stdout_json or "{}")
    out = []
    for fr in data.get("frames", []):
        try: out.append(float(fr["pts_time"]))
        except (KeyError, TypeError, ValueError): continue
    return sorted(out)

def _build_scenes(cut_times, duration, min_len) -> dict:
    bounds = [0.0]
    for t in cut_times:
        if t - bounds[-1] >= min_len and t < duration:
            bounds.append(t)
    if duration > bounds[-1]:
        bounds.append(duration)
    scenes = [{"index": i, "start_seconds": round(bounds[i], 3),
               "end_seconds": round(bounds[i+1], 3),
               "duration_seconds": round(bounds[i+1] - bounds[i], 3)}
              for i in range(len(bounds) - 1)]
    return {"cuts": [round(b, 3) for b in bounds[1:-1]], "scenes": scenes}

_EBUR_LINE = re.compile(r"t:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+|-inf)", re.IGNORECASE)
_SILENCE_LUFS = -120.0

def _parse_ebur128(stderr: str) -> list[tuple[float, float]]:
    out = []
    for ln in stderr.splitlines():
        m = _EBUR_LINE.search(ln)
        if m:
            lufs = _SILENCE_LUFS if m.group(2).lower() == "-inf" else float(m.group(2))
            out.append((float(m.group(1)), max(_SILENCE_LUFS, lufs)))
    return out

def _energy_profile(samples, threshold) -> dict:
    """Per-second averages of ~10/s momentary LUFS + summary; empty → zeroed shape.

    Fixed per-second buckets (floor(max t)+1) — resolution enough for a
    loudness strip and bounded memory; finer bins would need a downsample step.
    """
    if not samples:
        return {"analysis": {"threshold_lufs": threshold, "total_seconds": 0,
                             "active_seconds": 0, "quiet_intro_seconds": 0,
                             "peak_loudness_at_seconds": None,
                             "peak_loudness_lufs": None},
                "energy_profile": []}
    total = math.floor(max(t for t, _ in samples)) + 1
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    for t, lufs in samples:
        b = math.floor(t)            # pts_time >= 0 → bucket = whole second
        sums[b] = sums.get(b, 0.0) + lufs
        counts[b] = counts.get(b, 0) + 1
    profile: list[dict] = []
    peak_lufs = None
    peak_at: int | None = None
    active = 0
    quiet_intro = 0
    leading = True
    for s in range(total):
        avg = sums[s] / counts[s] if counts.get(s) else _SILENCE_LUFS
        is_active = avg > threshold
        if is_active:
            active += 1
            leading = False
        elif leading:
            quiet_intro += 1
        if peak_lufs is None or avg > peak_lufs:
            peak_lufs, peak_at = avg, s
        profile.append({"time_seconds": s, "loudness_lufs": round(avg, 1), "active": is_active})
    return {"analysis": {"threshold_lufs": threshold, "total_seconds": total,
                         "active_seconds": active, "quiet_intro_seconds": quiet_intro,
                         "peak_loudness_at_seconds": peak_at,
                         "peak_loudness_lufs": round(peak_lufs, 1)},
            "energy_profile": profile}

async def _run_capture(job: "Job", argv: list[str]) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(f"{argv[0]} binary not found on PATH")
    job.proc = proc
    out_b, err_b = await proc.communicate()
    job.proc = None
    return (proc.returncode or 0, out_b.decode(errors="replace"),
            err_b.decode(errors="replace"))

async def analyze_scene_detect(inputs, p, job) -> dict:
    rc, stdout, stderr = await _run_capture(job, _scene_argv(inputs[0], p["threshold"]))
    if rc != 0:
        for ln in stderr.splitlines()[-20:]: job.logs.append(ln)
        raise RuntimeError(f"ffprobe exited {rc}")
    built = _build_scenes(_parse_scene_frames(stdout), p["durations"][0], p["min_scene_len"])
    job.logs.append(f"scene_detect: {len(built['cuts'])} cut(s), {len(built['scenes'])} scene(s)")
    return {"op": "scene_detect", "input": str(inputs[0]), "threshold": p["threshold"],
            "min_scene_length_seconds": p["min_scene_len"],
            "duration_seconds": round(p["durations"][0], 3),
            "scene_count": len(built["scenes"]), **built}

async def analyze_audio_energy(inputs, p, job) -> dict:
    rc, _out, stderr = await _run_capture(job, _energy_argv(inputs[0]))
    if rc != 0:
        for ln in stderr.splitlines()[-20:]: job.logs.append(ln)
        raise RuntimeError(f"ffmpeg exited {rc}")
    samples = _parse_ebur128(stderr)
    if not samples:
        raise RuntimeError("no ebur128 samples parsed from ffmpeg output")
    prof = _energy_profile(samples, p["energy_threshold"])
    a = prof["analysis"]
    job.logs.append(f"audio_energy: {a['active_seconds']}/{a['total_seconds']}s active, "
                    f"peak {a['peak_loudness_lufs']} LUFS")
    return {"op": "audio_energy", "input": str(inputs[0]),
            "audio_duration_seconds": round(p["durations"][0], 3), **prof}

ANALYZERS = {"scene_detect": analyze_scene_detect, "audio_energy": analyze_audio_energy}


BUILDERS = {
    "transcode_h264": transcode_h264,
    "concat": concat,
    "lut": lut,
    "xfade": xfade,
    "transcode_dnxhr": transcode_dnxhr,
    # EFFECTS (single clip):
    "fade": fade,
    "speed": speed,
    "denoise": denoise,
    "sharpen": sharpen,
    "vignette": vignette,
    "grain": grain,
    "mirrorh": mirrorh,
    "mirrorv": mirrorv,
    "grayscale": grayscale,
    # LOOK (single clip, 2026-07-20):
    "sepia": sepia,
    "warm": warm,
    "cool": cool,
    "vibrance": vibrance,
    "brightness_up": brightness_up,
    "brightness_down": brightness_down,
    "contrast_up": contrast_up,
    "contrast_down": contrast_down,
    "saturation_up": saturation_up,
    "saturation_down": saturation_down,
    # EFFECTS (single clip, 2026-07-20):
    "negative": negative,
    "posterize": posterize,
    "pixelize": pixelize,
    "cartoon": cartoon,
    "deband": deband,
    "deflicker": deflicker,
    "sharpen_strong": sharpen_strong,
    "denoise_heavy": denoise_heavy,
    # FRAME (single clip):
    "fit_aspect": fit_aspect,
    # AUDIO (single clip):
    "loudnorm": loudnorm,
    "volume": volume,
}

# ---------------------------------------------------------------- op catalog (single source of truth)
# Adding an op = (1) write a builder fn above, (2) append ONE row here, (3) register
# it in BUILDERS. The import-time assert below keeps this list, BUILDERS, _MULTI, and
# AUDIO_OPS from drifting — so a new no-param effect grows the Video Lab menu with
# ZERO frontend edits (the UI renders /api/ffmpeg/ops verbatim, grouped by `cat`).
_CATALOG: list[dict] = [
    {"id": "concat",         "label": "Butt-cut stitch",    "cat": "ASSEMBLY",  "needs": "multi",  "hint": "≥2 clips, in order"},
    {"id": "xfade",          "label": "Crossfade chain",    "cat": "ASSEMBLY",  "needs": "multi",  "hint": "≥2 clips + transition"},
    {"id": "lut",            "label": "Apply LUT",          "cat": "LOOK",      "needs": "single", "hint": "one clip + a .cube LUT"},
    {"id": "sepia",          "label": "Sepia",              "cat": "LOOK",      "needs": "single", "hint": "vintage brown tone"},
    {"id": "warm",           "label": "Warm temp",          "cat": "LOOK",      "needs": "single", "hint": "toward tungsten 4500K"},
    {"id": "cool",           "label": "Cool temp",          "cat": "LOOK",      "needs": "single", "hint": "toward blue 8500K"},
    {"id": "vibrance",       "label": "Vibrance",           "cat": "LOOK",      "needs": "single", "hint": "boost muted colors"},
    {"id": "brightness_up",  "label": "Brightness +",       "cat": "LOOK",      "needs": "single", "hint": "lift +0.06"},
    {"id": "brightness_down","label": "Brightness −",       "cat": "LOOK",      "needs": "single", "hint": "lower −0.06"},
    {"id": "contrast_up",    "label": "Contrast +",         "cat": "LOOK",      "needs": "single", "hint": "×1.15"},
    {"id": "contrast_down",  "label": "Contrast −",         "cat": "LOOK",      "needs": "single", "hint": "×0.9"},
    {"id": "saturation_up",  "label": "Saturation +",       "cat": "LOOK",      "needs": "single", "hint": "×1.3"},
    {"id": "saturation_down","label": "Saturation −",       "cat": "LOOK",      "needs": "single", "hint": "×0.7"},
    {"id": "fade",           "label": "Fade in/out",        "cat": "EFFECTS",   "needs": "single", "hint": "0.5s in + out"},
    {"id": "speed",          "label": "Speed ramp",         "cat": "EFFECTS",   "needs": "single", "hint": "2× fast / 0.5× slow"},
    {"id": "denoise",        "label": "Denoise",            "cat": "EFFECTS",   "needs": "single", "hint": "hqdn3d"},
    {"id": "sharpen",        "label": "Sharpen",            "cat": "EFFECTS",   "needs": "single", "hint": "unsharp"},
    {"id": "vignette",       "label": "Vignette",           "cat": "EFFECTS",   "needs": "single", "hint": "darken edges"},
    {"id": "grain",          "label": "Film grain",         "cat": "EFFECTS",   "needs": "single", "hint": "add grain"},
    {"id": "mirrorh",        "label": "Mirror H",           "cat": "EFFECTS",   "needs": "single", "hint": "hflip"},
    {"id": "mirrorv",        "label": "Mirror V",           "cat": "EFFECTS",   "needs": "single", "hint": "vflip"},
    {"id": "grayscale",      "label": "Grayscale",          "cat": "EFFECTS",   "needs": "single", "hint": "desaturate"},
    {"id": "negative",       "label": "Negative",           "cat": "EFFECTS",   "needs": "single", "hint": "invert colors"},
    {"id": "posterize",      "label": "Posterize",          "cat": "EFFECTS",   "needs": "single", "hint": "6 levels/channel"},
    {"id": "pixelize",       "label": "Pixelize",           "cat": "EFFECTS",   "needs": "single", "hint": "16px mosaic"},
    {"id": "cartoon",        "label": "Cartoon edges",      "cat": "EFFECTS",   "needs": "single", "hint": "edge-colormix look"},
    {"id": "deband",         "label": "Deband",             "cat": "EFFECTS",   "needs": "single", "hint": "smooth banding"},
    {"id": "deflicker",      "label": "Deflicker",          "cat": "EFFECTS",   "needs": "single", "hint": "even out flicker"},
    {"id": "sharpen_strong", "label": "Sharpen strong",     "cat": "EFFECTS",   "needs": "single", "hint": "unsharp 7×7 ×2.5"},
    {"id": "denoise_heavy",  "label": "Denoise heavy",      "cat": "EFFECTS",   "needs": "single", "hint": "hqdn3d ×2"},
    {"id": "fit_aspect",     "label": "Fit aspect (blur)",  "cat": "FRAME",     "needs": "single", "hint": "9:16 → 16:9 blur-fill"},
    {"id": "loudnorm",       "label": "Loudness normalize", "cat": "AUDIO",     "needs": "single", "audio": True, "hint": "EBU R128 −16 LUFS"},
    {"id": "volume",         "label": "Volume/gain",        "cat": "AUDIO",     "needs": "single", "audio": True, "hint": "×2 gain"},
    {"id": "transcode_h264", "label": "H.264 master",       "cat": "TRANSCODE", "needs": "single", "hint": "CRF 18 master"},
    {"id": "transcode_dnxhr","label": "DNxHR (Resolve)",    "cat": "TRANSCODE", "needs": "single", "hint": "edit intermediate"},
    {"id": "scene_detect",  "label": "Scene detect",        "cat": "ANALYZE",    "needs": "single", "analysis": True, "hint": "cut list → JSON, no render"},
    {"id": "audio_energy",  "label": "Audio energy",         "cat": "ANALYZE",    "needs": "single", "analysis": True, "audio": True, "hint": "loudness profile → JSON"},
]
assert not set(BUILDERS) & set(ANALYZERS), "an op cannot be both builder and analyzer"
assert {c["id"] for c in _CATALOG} == set(BUILDERS) | set(ANALYZERS), \
    "BUILDERS ∪ ANALYZERS and _CATALOG drifted — sync them"

# ops that take exactly one clip vs. ops that need >= 2 (ordered)
_MULTI = {c["id"] for c in _CATALOG if c["needs"] == "multi"}
# ops that require an audio stream — guarded in create_job after ffprobe
AUDIO_OPS = {c["id"] for c in _CATALOG if c.get("audio")}
ANALYSIS_OPS = {c["id"] for c in _CATALOG if c.get("analysis")}
assert ANALYSIS_OPS == set(ANALYZERS), "analysis flag and ANALYZERS drifted"


def _total_duration(op: str, probes: list[tuple[float, bool]], p: dict) -> float:
    durs = [d for d, _ in probes]
    if op == "xfade":
        # §2: total = Σdur − (n−1)·transition (each crossfade overlaps the join)
        return max(0.0, sum(durs) - (len(durs) - 1) * p["transition"])
    if op == "speed":
        # output time = input / factor (progress tracks out_time, which is output time)
        return max(0.0, sum(durs) / (p.get("speed") or 2.0))
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
    result: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "op": self.op,
            "status": self.status,
            "progress": self.progress,
            "output_path": self.output_path,
            "error": self.error,
            "logs": list(self.logs),
            "result": self.result,
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


async def _execute_analysis(job: Job, op: str, inputs: list[Path],
                            params: dict, out: Path) -> None:
    """Analysis twin of _execute: same lock + lifecycle; dict result + JSON
    sidecar instead of a rendered file; no incremental progress."""
    async with _LOCK:
        if job.cancel:
            job.status = "cancelled"; return
        job.status = "running"
        try:
            result = await ANALYZERS[op](inputs, params, job)
        except RuntimeError as e:
            if job.cancel: job.status = "cancelled"
            else: job.status, job.error = "error", str(e)
            return
        if job.cancel:
            job.status = "cancelled"; return
        try:
            out.write_text(json.dumps(result, indent=2))
        except OSError as e:
            job.logs.append(f"sidecar write failed: {e}"); job.output_path = None
        # inline copy capped so the 1s poll doesn't ship megabytes (sidecar keeps all)
        slim = dict(result)
        prof = slim.get("energy_profile")
        if isinstance(prof, list) and len(prof) > 3600:
            slim["energy_profile"] = prof[:3600]
            slim["energy_profile_truncated"] = True
        job.result = slim
        job.status, job.progress = "done", 100


# ---------------------------------------------------------------- output naming


def _output_path(op: str, out_dir: Path) -> Path:
    ext = ".json" if op in ANALYSIS_OPS else (".mov" if op == "transcode_dnxhr" else ".mp4")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return out_dir / f"{op}_{ts}{ext}"


def _resolve_output_dir(raw: str | None) -> Path:
    """Resolve + confine the render output dir (default ``_default_output_dir``),
    creating it on demand. Raises 400 if a client-supplied dir escapes ~."""
    d = Path(raw).expanduser().resolve() if raw else _default_output_dir()
    if not _under(d, [_browse_root()]):
        raise HTTPException(400, "output dir outside the browse root")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- API models


class JobBody(BaseModel):
    op: str
    files: list[str]
    lut: str | None = None
    transition: float | None = None        # xfade crossfade duration (s)
    transition_style: str | None = None    # xfade transition name (fade/wipeleft/…)
    fade: float | None = None              # fade in/out duration (s)
    speed: float | None = None             # speed factor (2.0 = 2× fast)
    gain: float | None = None              # audio gain factor (2.0 = 2×)
    threshold: float | None = None         # scene_detect scene score 0..1
    min_scene_len: float | None = None     # scene_detect min scene length (s)
    energy_threshold: float | None = None  # audio_energy active threshold (LUFS)
    output_dir: str | None = None  # UI-picked render folder; None → default


# ---------------------------------------------------------------- endpoints


@router.get("/api/ffmpeg/panel")
def panel_config() -> dict:
    """Video Lab bootstrap: browse root, default output dir, and the configured
    media_dirs the UI seeds the workspace with on first load."""
    return {
        "browse_root": str(_browse_root()),
        "default_output": str(_default_output_dir()),
        "media_dirs": [str(r) for r in _media_roots() if r.is_dir()],
    }


@router.get("/api/ffmpeg/browse")
def browse(path: str | None = None) -> dict:
    """List immediate sub-folders (+ this folder's direct video count) under a
    dir, for the ADD FOOTAGE / output-folder picker. Confined under the browse
    root; hidden dot-entries skipped."""
    root = _browse_root()
    target = Path(path).expanduser().resolve() if path else root
    if not _under(target, [root]):
        raise HTTPException(400, "path outside the browse root")
    if not target.is_dir():
        raise HTTPException(404, "not a directory")
    dirs: list[dict] = []
    videos = 0
    try:
        for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                dirs.append({"name": child.name, "path": str(child)})
            elif child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                videos += 1
    except PermissionError:
        raise HTTPException(403, "permission denied")
    parent = str(target.parent) if target != root and _under(target.parent, [root]) else None
    return {"path": str(target), "root": str(root), "parent": parent, "dirs": dirs, "video_count": videos}


@router.get("/api/ffmpeg/files")
def files(dir: list[str] = Query(default=[])) -> dict:
    """Video files (recursive) under the requested ``dir`` params, tagged by the
    folder name. No ``dir`` → the configured ``media_dirs`` (back-compat). Each
    requested dir is confined under the browse root; strays are skipped, and
    missing dirs skip silently (never 500)."""
    if dir:
        roots = [Path(d).expanduser().resolve() for d in dir]
        br = _browse_root()
        roots = [r for r in roots if _under(r, [br])]
    else:
        roots = _media_roots()
    out: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue  # e.g. ~/Downloads/B-Rolls absent — never 500
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and str(p) not in seen:
                seen.add(str(p))
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


@router.get("/api/ffmpeg/transitions")
def list_transitions() -> dict:
    """Live xfade transition names for the TRANSITION dropdown (auto-updates with ffmpeg)."""
    return {"transitions": _xfade_transitions()}


@router.get("/api/ffmpeg/ops")
def list_ops() -> dict:
    """Op catalog for the Video Lab menu — grouped by `cat` client-side. Single
    source of truth: BUILDERS and this list are kept in sync by an import-time
    assert, so the menu grows by appending one row (no frontend edit needed)."""
    return {"ops": _CATALOG}


def _validate(op: str, body: JobBody) -> tuple[list[Path], Path, dict]:
    """Resolve + confine paths, check op arity, collect op params. No ffprobe."""
    if op not in BUILDERS and op not in ANALYZERS:
        raise HTTPException(400, f"unknown op {op!r}; want one of {sorted(set(BUILDERS) | set(ANALYZERS))}")

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
        params["style"] = body.transition_style or "fade"
    if op == "fade":
        params["fade"] = body.fade if body.fade and body.fade > 0 else 0.5
    if op == "speed":
        # atempo chain ceiling is 16; clamp negatives/zero to the 2× default
        params["speed"] = min(16.0, max(0.25, body.speed)) if body.speed else 2.0
    if op == "volume":
        params["gain"] = body.gain if body.gain and body.gain > 0 else 2.0
    if op == "scene_detect":
        t = body.threshold if body.threshold is not None else 0.3
        params["threshold"] = min(1.0, max(0.0, t))
        m = body.min_scene_len if body.min_scene_len is not None else 1.0
        params["min_scene_len"] = max(0.0, m)
    if op == "audio_energy":
        params["energy_threshold"] = body.energy_threshold if body.energy_threshold is not None else -40.0
    out_dir = _resolve_output_dir(body.output_dir)
    return inputs, _output_path(op, out_dir), params


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

    if op in AUDIO_OPS and not params["audio"]:
        raise HTTPException(400, f"{op} needs an audio stream; this clip has none")

    jid = uuid4().hex[:8]          # shared by both paths
    if op in ANALYSIS_OPS:
        job = Job(id=jid, op=op, output_path=str(out))
        _JOBS[jid] = job
        task = asyncio.create_task(_execute_analysis(job, op, inputs, params, out))
        _BG.add(task); task.add_done_callback(_BG.discard)
        return {"id": jid}

    argv = BUILDERS[op](inputs, out, params)
    # global flags the progress pump depends on: machine-readable key=value
    # progress on stdout, human stats (which would corrupt it) off
    argv[1:1] = ["-progress", "pipe:1", "-nostats"]
    total_us = int(_total_duration(op, probes, params) * 1_000_000)

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
