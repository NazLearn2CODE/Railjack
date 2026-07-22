"""Path confinement (``app/ffmpeg_jobs.py``).

Every client path must resolve under a configured root. ``_safe_lut`` confines
to ``lut_dir`` but does NOT validate the extension (``LUT_EXTS`` is enforced
only by the ``luts()`` listing endpoint) — locked here as current behavior.
"""

import pytest

from app import ffmpeg_jobs


def test_safe_input_rejects_traversal(ffmpeg_opts):
    # Enough `..` to climb above the browse root (~) to the real /etc/passwd,
    # which is outside every input root. (A `../` that lands back inside ~ is
    # intentionally allowed now — footage can live anywhere under home.)
    with pytest.raises(ValueError):
        ffmpeg_jobs._safe_input("../" * 12 + "etc/passwd")


def test_safe_input_rejects_absolute(ffmpeg_opts):
    with pytest.raises(ValueError):
        ffmpeg_jobs._safe_input("/etc/passwd")


def test_safe_input_accepts_under_root(ffmpeg_opts):
    clip = ffmpeg_opts["media"] / "clips" / "a.mp4"
    clip.parent.mkdir(parents=True)
    clip.touch()
    assert ffmpeg_jobs._safe_input(str(clip)) == clip.resolve()


def test_safe_lut_rejects_outside_root(ffmpeg_opts):
    with pytest.raises(ValueError):
        ffmpeg_jobs._safe_lut("/etc/passwd")


def test_safe_lut_accepts_under_root(ffmpeg_opts):
    lut = ffmpeg_opts["luts"] / "warm.cube"
    lut.touch()
    assert ffmpeg_jobs._safe_lut(str(lut)) == lut.resolve()


def test_safe_lut_does_not_validate_extension(ffmpeg_opts):
    # ponytail: _safe_lut confines to root only; it never checks the suffix.
    # ffmpeg rejects a bad LUT at run time (rc!=0 -> error), so this is a UX
    # gap, not a confinement hole. Locking current behavior; upgrade path is
    # adding a LUT_EXTS check here if early rejection is wanted.
    weird = ffmpeg_opts["luts"] / "not-a-lut.txt"
    weird.touch()
    assert ffmpeg_jobs._safe_lut(str(weird)) == weird.resolve()


def test_api_escaping_path_returns_400(ffmpeg_opts):
    ffmpeg_jobs._JOBS.clear()  # never trip the one-job-at-a-time 409
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/ffmpeg/jobs",
        json={"op": "transcode_h264", "files": ["../" * 12 + "etc/passwd"]},
    )
    assert r.status_code == 400  # _validate raises before ffprobe is ever called


def test_api_escaping_path_returns_400_scene_detect(ffmpeg_opts):
    """Analysis ops take the same confinement path as builders — a traversal
    input is rejected in _validate before ffprobe/the analyzer run."""
    ffmpeg_jobs._JOBS.clear()
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.post(
        "/api/ffmpeg/jobs",
        json={"op": "scene_detect", "files": ["../" * 12 + "etc/passwd"]},
    )
    assert r.status_code == 400
