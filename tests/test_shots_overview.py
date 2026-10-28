from pathlib import Path
from types import SimpleNamespace

import pytest

from afterwatch import shots


def test_format_time_label():
    assert shots.format_time_label(0) == "00:00"
    assert shots.format_time_label(75) == "01:15"
    assert shots.format_time_label(3723) == "1:02:03"


def test_shot_filename_under_and_over_hour():
    assert shots.shot_filename("abc123DEF-_", 260) == "shot-abc123DEF-_-0420.jpg"
    assert shots.shot_filename("abc123DEF-_", 3723) == "shot-abc123DEF-_-10203.jpg"


def test_best_storyboard_picks_fewest_fragments():
    info = {"formats": [
        {"format_note": "video", "width": 1920, "height": 1080},
        {"format_note": "storyboard", "format_id": "sb0", "rows": 3, "columns": 3,
         "width": 320, "height": 180,
         "fragments": [{"url": "u", "duration": 17.75}] * 12},
        {"format_note": "storyboard", "format_id": "sb3", "rows": 10, "columns": 10,
         "width": 48, "height": 27,
         "fragments": [{"url": "whole", "duration": 213.0}]},
    ]}
    sb = shots.best_storyboard(info)
    assert sb["format_id"] == "sb3"  # single fragment covers whole video in one image


def test_best_storyboard_none_when_absent():
    assert shots.best_storyboard({"formats": [{"format_note": "video"}]}) is None
    assert shots.best_storyboard({}) is None


def test_tile_timestamps_row_major_first_fragment():
    sb = {"rows": 2, "columns": 2, "fragments": [{"url": "u", "duration": 40.0}]}
    # 4 tiles over 40s -> 10s each, starting at 0
    assert shots.tile_timestamps(sb) == [0, 10, 20, 30]


def test_build_overview_uses_storyboard(tmp_path, monkeypatch):
    info = {"formats": [
        {"format_note": "storyboard", "format_id": "sb3", "rows": 2, "columns": 2,
         "width": 48, "height": 27,
         "fragments": [{"url": "http://sprite", "duration": 40.0}]},
    ]}

    class FakeYDL:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download): return info

    monkeypatch.setattr(shots.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(shots, "_fetch_bytes", lambda url: b"\xff\xd8jpegbytes")

    ov = shots.build_overview("vid", "http://y", 40, tmp_path)
    assert ov is not None
    assert ov.image_path.exists()
    assert ov.tile_seconds == [0, 10, 20, 30]


def test_build_overview_falls_back_to_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_extract_info", lambda url: {"formats": []})

    def fake_ffmpeg_sheet(url, duration, out_path, workdir):
        out_path.write_bytes(b"\xff\xd8sheet")
        return [0, 30, 60]

    monkeypatch.setattr(shots, "_ffmpeg_contact_sheet", fake_ffmpeg_sheet)
    ov = shots.build_overview("vid", "http://y", 90, tmp_path)
    assert ov is not None and ov.tile_seconds == [0, 30, 60]


def test_build_overview_none_when_all_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(shots, "_extract_info", lambda url: {"formats": []})
    monkeypatch.setattr(shots, "_ffmpeg_contact_sheet", lambda *a, **k: None)
    assert shots.build_overview("vid", "http://y", 90, tmp_path) is None


def test_ffmpeg_contact_sheet_time_based_mapping(tmp_path, monkeypatch):
    out_path = tmp_path / "sheet.jpg"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ffmpeg":
            out_path.write_bytes(b"\xff\xd8sheet")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(shots.subprocess, "run", fake_run)

    seconds = shots._ffmpeg_contact_sheet("http://y", 120, out_path, tmp_path)

    assert seconds == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110]

    ffmpeg_cmd = next(c for c in calls if c[0] == "ffmpeg")
    vf_index = ffmpeg_cmd.index("-vf") + 1
    vf = ffmpeg_cmd[vf_index]
    assert "fps=1/" in vf
    assert "mod(" not in vf


def test_ffmpeg_contact_sheet_returns_none_on_failure(tmp_path, monkeypatch):
    out_path = tmp_path / "sheet.jpg"

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(shots.subprocess, "run", fake_run)

    assert shots._ffmpeg_contact_sheet("http://y", 120, out_path, tmp_path) is None
