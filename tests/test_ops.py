"""New single-clip ops + xfade style + op catalog (``app/ffmpeg_jobs.py``).

M6.4 cut: EFFECTS (fade/speed/denoise/sharpen/vignette/grain/mirror/grayscale),
AUDIO (loudnorm/volume), and the 59-style xfade TRANSITION picker. Builders are
pure argv constructors → each case asserts on the argv list (same pattern as
test_xfade.py). ``test_catalog_matches_builders`` guards the growth invariant:
the menu catalog, BUILDERS, _MULTI, AUDIO_OPS must stay in sync.
"""

from pathlib import Path

import pytest

from app import ffmpeg_jobs

IN = [Path("/m/clip.mp4")]
OUT = Path("/m/out.mp4")


def _p(**kw):
    base = {"audio": True, "durations": [5.0]}
    base.update(kw)
    return base


def _vf(argv):
    return argv[argv.index("-vf") + 1]


def _fc(argv):
    return argv[argv.index("-filter_complex") + 1]


# -- catalog / growth invariant -------------------------------------------

def test_catalog_matches_builders():
    """The UI catalog and the runner's BUILDERS must name the same ops."""
    assert {c["id"] for c in ffmpeg_jobs._CATALOG} == set(ffmpeg_jobs.BUILDERS)


def test_multi_and_audio_derive_from_catalog():
    assert ffmpeg_jobs._MULTI == {"concat", "xfade"}
    assert ffmpeg_jobs.AUDIO_OPS == {"loudnorm", "volume"}


def test_every_catalog_op_has_a_builder_and_metadata():
    for c in ffmpeg_jobs._CATALOG:
        assert c["id"] in ffmpeg_jobs.BUILDERS
        assert c["needs"] in ("single", "multi")
        assert {"id", "label", "cat", "needs", "hint"} <= set(c)


# -- xfade style -----------------------------------------------------------

def test_xfade_style_defaults_to_fade():
    inputs = [Path("/m/a.mp4"), Path("/m/b.mp4")]
    argv = ffmpeg_jobs.xfade(inputs, OUT, {"transition": 0.5, "durations": [4.0, 3.0], "audio": False})
    assert "xfade=transition=fade:" in _fc(argv)


def test_xfade_style_wipeleft_replaces_fade():
    inputs = [Path("/m/a.mp4"), Path("/m/b.mp4")]
    p = {"transition": 0.5, "style": "wipeleft", "durations": [4.0, 3.0], "audio": False}
    argv = ffmpeg_jobs.xfade(inputs, OUT, p)
    fc = _fc(argv)
    assert "xfade=transition=wipeleft:" in fc
    assert "transition=fade" not in fc


# -- fade (§5) -------------------------------------------------------------

def test_fade_in_out_video_and_audio():
    argv = ffmpeg_jobs.fade(IN, OUT, _p(fade=0.5))
    vf = _vf(argv)
    assert "fade=t=in:st=0:d=0.5" in vf
    assert "fade=t=out:st=4.5:d=0.5" in vf  # dur 5 − 0.5
    af = argv[argv.index("-af") + 1]
    assert "afade=t=in:st=0:d=0.5" in af
    assert "afade=t=out:st=4.5:d=0.5" in af


def test_fade_no_audio_skips_afade():
    argv = ffmpeg_jobs.fade(IN, OUT, _p(fade=0.5, audio=False))
    assert "-af" not in argv
    assert "fade=t=out:st=4.5" in _vf(argv)


# -- speed (§6) ------------------------------------------------------------

def test_speed_2x_setpts_and_atempo():
    argv = ffmpeg_jobs.speed(IN, OUT, _p(speed=2.0))
    fc = _fc(argv)
    assert "setpts=0.5*PTS" in fc
    assert "atempo=2.0" in fc


def test_speed_4x_chains_atempo():
    argv = ffmpeg_jobs.speed(IN, OUT, _p(speed=4.0))
    fc = _fc(argv)
    assert "atempo=2.0,atempo=2.0" in fc
    assert "setpts=0.25*PTS" in fc


def test_speed_half():
    argv = ffmpeg_jobs.speed(IN, OUT, _p(speed=0.5))
    fc = _fc(argv)
    assert "setpts=2.0*PTS" in fc
    assert "atempo=0.5" in fc


def test_speed_no_audio_uses_vf():
    argv = ffmpeg_jobs.speed(IN, OUT, _p(speed=2.0, audio=False))
    assert "-filter_complex" not in argv
    assert "setpts=0.5*PTS" in _vf(argv)


# -- effect factory (no-param taste effects) ------------------------------

@pytest.mark.parametrize("op,needle", [
    ("denoise", "hqdn3d"),
    ("sharpen", "unsharp"),
    ("vignette", "vignette"),
    ("grain", "noise="),
    ("mirrorh", "hflip"),
    ("mirrorv", "vflip"),
    ("grayscale", "hue=s=0"),
])
def test_effect_factory_applies_filter_and_master(op, needle):
    argv = ffmpeg_jobs.BUILDERS[op](IN, OUT, _p())
    assert needle in _vf(argv)
    assert "libx264" in argv  # master block re-encodes


# -- audio ops -------------------------------------------------------------

def test_loudnorm_copies_video_and_filters_audio():
    argv = ffmpeg_jobs.loudnorm(IN, OUT, _p())
    af = argv[argv.index("-af") + 1]
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in af
    assert "libx264" not in argv
    assert argv[argv.index("-c:v") + 1] == "copy"


def test_volume_gain_factor():
    argv = ffmpeg_jobs.volume(IN, OUT, _p(gain=2.0))
    assert argv[argv.index("-af") + 1] == "volume=2.0"
    assert argv[argv.index("-c:v") + 1] == "copy"


# -- duration math + live transition list ---------------------------------

def test_total_duration_speed_halves():
    assert ffmpeg_jobs._total_duration("speed", [(4.0, True)], {"speed": 2.0}) == 2.0


def test_xfade_transitions_live_and_sorted_with_fade():
    ffmpeg_jobs._XFADE = None  # reset the cache so this probes the real binary
    names = ffmpeg_jobs._xfade_transitions()
    assert "fade" in names
    assert names == sorted(names)
    assert len(names) > 10  # this build has ~59
