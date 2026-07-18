"""Video Lab file browser + output-dir picker (``app/ffmpeg_jobs.py``).

The browser navigates sub-folders under a confined ``browse_root``; footage can
come from anywhere under it, while renders land in a confined output dir
(default ``~/Downloads/VDO Outputs``, here redirected to a tmp root).
"""

import pytest
from fastapi.testclient import TestClient

from app import ffmpeg_jobs
from app.main import app


@pytest.fixture
def browse_opts(monkeypatch, tmp_path):
    """Point browse_root/media_dirs/output_dir at a tmp tree.

    Layout:
      root/
        clips/a.mp4, b.mp4        (a media_dir; also a browsable sub-folder)
        clips/nested/c.mp4
        empty/                    (sub-folder, no videos)
        renders/                  (default output)
    """
    root = tmp_path / "root"
    clips = root / "clips"
    nested = clips / "nested"
    empty = root / "empty"
    renders = root / "renders"
    for d in (clips, nested, empty, renders):
        d.mkdir(parents=True)
    (clips / "a.mp4").touch()
    (clips / "b.mp4").touch()
    (nested / "c.mp4").touch()
    (clips / "notes.txt").touch()  # non-video ignored
    monkeypatch.setattr(
        "app.ffmpeg_jobs._OPTS",
        {
            "media_dirs": [str(clips)],
            "lut_dir": str(root / "luts"),
            "browse_root": str(root),
            "output_dir": str(renders),
        },
    )
    ffmpeg_jobs._JOBS.clear()
    return {"root": root, "clips": clips, "nested": nested, "empty": empty, "renders": renders}


def _client() -> TestClient:
    return TestClient(app)


def test_panel_config(browse_opts):
    r = _client().get("/api/ffmpeg/panel")
    assert r.status_code == 200
    body = r.json()
    assert body["browse_root"] == str(browse_opts["root"])
    assert body["default_output"] == str(browse_opts["renders"])
    assert body["media_dirs"] == [str(browse_opts["clips"])]


def test_browse_root_lists_subdirs_and_counts(browse_opts):
    r = _client().get("/api/ffmpeg/browse")  # no path -> root
    assert r.status_code == 200
    body = r.json()
    assert body["parent"] is None  # at root
    names = [d["name"] for d in body["dirs"]]
    assert names == ["clips", "empty", "renders"]  # sorted, no hidden
    assert body["video_count"] == 0  # no videos directly in root


def test_browse_counts_direct_videos_only(browse_opts):
    r = _client().get("/api/ffmpeg/browse", params={"path": str(browse_opts["clips"])})
    body = r.json()
    assert body["video_count"] == 2  # a.mp4, b.mp4 — not nested/c.mp4
    assert [d["name"] for d in body["dirs"]] == ["nested"]
    assert body["parent"] == str(browse_opts["root"])


def test_browse_rejects_outside_root(browse_opts):
    r = _client().get("/api/ffmpeg/browse", params={"path": "/etc"})
    assert r.status_code == 400


def test_browse_404_on_nondir(browse_opts):
    r = _client().get("/api/ffmpeg/browse", params={"path": str(browse_opts["clips"] / "a.mp4")})
    assert r.status_code == 404


def test_files_recurses_requested_dir(browse_opts):
    r = _client().get("/api/ffmpeg/files", params={"dir": str(browse_opts["clips"])})
    names = sorted(f["name"] for f in r.json()["files"])
    assert names == ["a.mp4", "b.mp4", "c.mp4"]  # recursive; notes.txt excluded


def test_files_ignores_out_of_scope_dir(browse_opts):
    r = _client().get("/api/ffmpeg/files", params={"dir": "/etc"})
    assert r.json()["files"] == []  # stray dir dropped, no 500


def test_files_no_dir_falls_back_to_media_dirs(browse_opts):
    r = _client().get("/api/ffmpeg/files")
    names = sorted(f["name"] for f in r.json()["files"])
    assert names == ["a.mp4", "b.mp4", "c.mp4"]  # media_dirs = clips


def test_output_dir_outside_root_rejected(browse_opts):
    r = _client().post(
        "/api/ffmpeg/jobs",
        json={"op": "transcode_h264", "files": [str(browse_opts["clips"] / "a.mp4")],
              "output_dir": "/tmp/evil"},
    )
    assert r.status_code == 400


def test_resolve_output_dir_default_created(browse_opts):
    import shutil
    shutil.rmtree(browse_opts["renders"])  # ensure created on demand
    d = ffmpeg_jobs._resolve_output_dir(None)
    assert d == browse_opts["renders"].resolve()
    assert d.is_dir()


def test_input_accepts_footage_anywhere_under_browse_root(browse_opts):
    # a file NOT in media_dirs but under browse_root (added via ADD FOOTAGE)
    extra = browse_opts["empty"] / "d.mp4"
    extra.touch()
    assert ffmpeg_jobs._safe_input(str(extra)) == extra.resolve()
