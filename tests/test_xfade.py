"""xfade offset math (``app/ffmpeg_jobs.py``).

§2 formula: offset_k = Σdur[0..k] − (k+1)·d. Ground truth (verified live in M4):
durations [4, 3, 5], transition 0.5 → offsets [3.5, 6.0], total 11.0.
"""

from pathlib import Path

from app import ffmpeg_jobs


def _xfade(durs, audio):
    inputs = [Path(f"/m/c{i}.mp4") for i in range(len(durs))]
    p = {"transition": 0.5, "durations": list(durs), "audio": audio}
    return ffmpeg_jobs.xfade(inputs, Path("/m/out.mp4"), p), p


def _fc(argv):
    return argv[argv.index("-filter_complex") + 1]


def test_offsets_three_clips():
    argv, _ = _xfade([4.0, 3.0, 5.0], audio=False)
    fc = _fc(argv)
    assert "offset=3.500" in fc
    assert "offset=6.000" in fc
    assert "[vout]" in fc


def test_total_duration_xfade():
    probes = [(4.0, True), (3.0, True), (5.0, True)]
    assert ffmpeg_jobs._total_duration("xfade", probes, {"transition": 0.5}) == 11.0


def test_audio_true_adds_acrossfade_and_aout():
    argv, _ = _xfade([4.0, 3.0, 5.0], audio=True)
    fc = _fc(argv)
    assert "acrossfade=d=0.5" in fc
    assert "[aout]" in fc
    assert "[aout]" in argv  # mapped


def test_audio_false_maps_only_vout():
    argv, _ = _xfade([4.0, 3.0, 5.0], audio=False)
    fc = _fc(argv)
    assert "[aout]" not in fc
    assert "acrossfade" not in fc
    assert "[aout]" not in argv


def test_two_clips_go_straight_to_vout():
    argv, _ = _xfade([4.0, 3.0], audio=False)
    fc = _fc(argv)
    assert "[vx1" not in fc  # no intermediate label for the 2-clip case
    assert "[v0][v1]xfade" in fc
    assert fc.endswith("[vout]")
