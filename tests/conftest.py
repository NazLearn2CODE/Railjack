"""Shared fixtures.

`ffmpeg_opts` points the ffmpeg module's ``_OPTS`` at tmp dirs so path
confinement can be tested without touching real ``~/Videos`` / ``~/Downloads``
roots. ``_media_roots()``/``_lut_root()`` re-read ``_OPTS`` at call time, so
patching the module global is enough.
"""

import pytest

from app import catalog, session_stats


@pytest.fixture(autouse=True)
def _reset_session_caches():
    """Session telemetry + catalog keep process-global caches. Clear them between
    tests so ordering can't leak a cached reading into a test that expects a
    fresh fetch/fallback."""
    session_stats._max_seen.clear()
    session_stats._usage_cache.clear()
    session_stats._last_state.clear()
    catalog._cache = None
    yield


@pytest.fixture
def ffmpeg_opts(monkeypatch, tmp_path):
    media = tmp_path / "media"
    luts = tmp_path / "luts"
    out = tmp_path / "out"
    for d in (media, luts, out):
        d.mkdir()
    monkeypatch.setattr(
        "app.ffmpeg_jobs._OPTS",
        {"media_dirs": [str(media)], "lut_dir": str(luts), "output_dir": str(out)},
    )
    return {"media": media, "luts": luts, "out": out}
