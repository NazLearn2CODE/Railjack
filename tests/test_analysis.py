"""ANALYZE ops (``app/ffmpeg_jobs.py``) — pure helpers + the analysis runner.

No ffmpeg/ffprobe invoked: argv builders assert on tokens, parsers/asserters
feed canned strings, and ``_execute_analysis`` is driven with monkeypatched
fake analyzers (mirrors ``test_jobs.py``'s ``asyncio.run`` + fresh-Lock pattern).
"""

import asyncio
import json
from pathlib import Path

from app import ffmpeg_jobs
from app.ffmpeg_jobs import Job

IN = Path("/m/clip.mp4")


# -- argv builders ---------------------------------------------------------

def test_lavfi_escape():
    e = ffmpeg_jobs._lavfi_escape
    # space passes through both parser levels untouched
    assert e("a b.mp4") == "a b.mp4"
    # every special char ends up backslash-escaped in the final string;
    # ':' / "'" hit BOTH levels (double backslash), ',' / ';' / '[' / ']' only
    # the filtergraph level (single). One assertion per char keeps the intent
    # readable even though the exact escape count varies by level.
    for ch in [":", "'", ",", ";", "[", "]"]:
        out = e("x" + ch + "y")
        assert ("\\" + ch) in out, f"{ch!r} not escaped in {out!r}"


def test_scene_argv_tokens():
    argv = ffmpeg_jobs._scene_argv(IN, 0.3)
    assert argv[0] == "ffprobe"
    assert argv[argv.index("-f") + 1] == "lavfi"
    graph = argv[-1]
    assert graph.startswith("movie=")
    assert "select=gt(scene\\,0.3)" in graph   # backslash-comma, scene score


def test_energy_argv_tokens():
    argv = ffmpeg_jobs._energy_argv(IN)
    assert argv[0] == "ffmpeg"
    assert argv[argv.index("-af") + 1] == "ebur128"
    assert "-vn" in argv
    assert argv[-3:] == ["-f", "null", "-"]


# -- parsers ---------------------------------------------------------------

def test_parse_scene_frames_sorted_and_skips_garbage():
    js = json.dumps({"frames": [{"pts_time": "2.5"}, {"pts_time": "0.4"},
                                {"pts_time": "N/A"}, {"nope": 1}]})
    assert ffmpeg_jobs._parse_scene_frames(js) == [0.4, 2.5]


def test_parse_scene_frames_empty_output():
    # ffprobe produced no JSON (garbage/empty) → "{}" fallback → no frames
    assert ffmpeg_jobs._parse_scene_frames("") == []
    assert ffmpeg_jobs._parse_scene_frames(json.dumps({})) == []


def test_build_scenes_drops_short_and_oob():
    built = ffmpeg_jobs._build_scenes([0.4, 2.0, 2.5, 8.0], 10.0, 1.0)
    assert built["cuts"] == [2.0, 8.0]          # 0.4 (too short) & 2.5 (too short) dropped
    assert len(built["scenes"]) == 3
    assert built["scenes"][0] == {"index": 0, "start_seconds": 0.0,
                                  "end_seconds": 2.0, "duration_seconds": 2.0}


def test_build_scenes_no_cuts_is_one_scene():
    built = ffmpeg_jobs._build_scenes([], 10.0, 1.0)
    assert built["cuts"] == []
    assert len(built["scenes"]) == 1
    assert built["scenes"][0]["duration_seconds"] == 10.0


def test_build_scenes_drops_cut_at_or_past_duration():
    built = ffmpeg_jobs._build_scenes([10.0, 12.0], 10.0, 1.0)
    assert built["cuts"] == []
    assert len(built["scenes"]) == 1


def test_parse_ebur128_real_shaped_lines():
    stderr = (
        "[Parsed_ebur128_0 @ 0x7f1234] t: 0.099979 M: -120.7 S: -120.0 I: -120.0 LUFS\n"
        "[Parsed_ebur128_0 @ 0x7f1234] t: 1.045678 M: -inf\n"
        "junk line with no match\n"
        "[Parsed_ebur128_0 @ 0x7f1234] t: 2.5 M: -23.4\n"
    )
    # -120.7 floors to the -120 silence clamp, same as -inf
    assert ffmpeg_jobs._parse_ebur128(stderr) == [
        (0.099979, -120.0), (1.045678, -120.0), (2.5, -23.4),
    ]


# -- energy profile --------------------------------------------------------

def test_energy_profile_two_seconds():
    samples = [(0.1, -50.0), (0.2, -50.0), (1.1, -11.2), (1.2, -11.2)]
    prof = ffmpeg_jobs._energy_profile(samples, -40.0)
    a = prof["analysis"]
    assert a["total_seconds"] == 2          # floor(1.2)+1
    assert a["active_seconds"] == 1         # s=1 only (-11.2 > -40)
    assert a["quiet_intro_seconds"] == 1    # leading s=0 inactive
    assert a["peak_loudness_at_seconds"] == 1
    assert a["peak_loudness_lufs"] == -11.2
    assert len(prof["energy_profile"]) == 2
    assert prof["energy_profile"][0]["active"] is False
    assert prof["energy_profile"][1]["active"] is True


def test_energy_profile_empty_zeroed():
    prof = ffmpeg_jobs._energy_profile([], -40.0)
    assert prof["analysis"] == {
        "threshold_lufs": -40.0, "total_seconds": 0, "active_seconds": 0,
        "quiet_intro_seconds": 0, "peak_loudness_at_seconds": None,
        "peak_loudness_lufs": None,
    }
    assert prof["energy_profile"] == []


# -- _execute_analysis lifecycle (monkeypatched fake analyzers) ------------

def _run_analysis(monkeypatch, fake_analyzer, job, out, op="audio_energy"):
    monkeypatch.setitem(ffmpeg_jobs.ANALYZERS, op, fake_analyzer)

    async def _go():
        # ponytail: fresh Lock bound to THIS loop (module-global was made at import
        # with no loop) — same reason as test_jobs._run.
        ffmpeg_jobs._LOCK = asyncio.Lock()
        await ffmpeg_jobs._execute_analysis(
            job, op, [IN], {"durations": [5.0], "energy_threshold": -40.0}, out
        )

    asyncio.run(_go())


def test_analysis_done_writes_sidecar(tmp_path, monkeypatch):
    async def good(inputs, p, job):
        return {"op": "audio_energy",
                "energy_profile": [{"time_seconds": 0, "loudness_lufs": -50.0, "active": False}]}

    out = tmp_path / "audio_energy_1.json"
    job = Job(id="a1", op="audio_energy")
    _run_analysis(monkeypatch, good, job, out)
    assert job.status == "done"
    assert job.progress == 100
    assert job.result is not None and job.result["op"] == "audio_energy"
    assert out.exists()
    assert json.loads(out.read_text())["op"] == "audio_energy"


def test_analysis_runtime_error_sets_error(tmp_path, monkeypatch):
    async def boom(inputs, p, job):
        raise RuntimeError("ffmpeg exited 1")

    out = tmp_path / "audio_energy_2.json"
    job = Job(id="a2", op="audio_energy")
    _run_analysis(monkeypatch, boom, job, out)
    assert job.status == "error"
    assert job.error == "ffmpeg exited 1"
    assert not out.exists()


def test_analysis_cancel_before_run(tmp_path, monkeypatch):
    async def must_not_run(inputs, p, job):
        raise AssertionError("analyzer must not run when pre-cancelled")

    out = tmp_path / "audio_energy_3.json"
    job = Job(id="a3", op="audio_energy")
    job.cancel = True
    _run_analysis(monkeypatch, must_not_run, job, out)
    assert job.status == "cancelled"
    assert not out.exists()


def test_analysis_truncates_long_profile_inline(tmp_path, monkeypatch):
    full = [{"time_seconds": i, "loudness_lufs": -50.0, "active": False} for i in range(4000)]

    async def big(inputs, p, job):
        return {"op": "audio_energy", "energy_profile": full}

    out = tmp_path / "audio_energy_4.json"
    job = Job(id="a4", op="audio_energy")
    _run_analysis(monkeypatch, big, job, out)
    assert job.status == "done"
    # inline (polled) copy is capped + flagged; sidecar keeps the full 4000
    assert job.result["energy_profile_truncated"] is True
    assert len(job.result["energy_profile"]) == 3600
    data = json.loads(out.read_text())
    assert len(data["energy_profile"]) == 4000
    assert "energy_profile_truncated" not in data
