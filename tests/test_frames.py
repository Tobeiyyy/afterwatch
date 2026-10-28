from pathlib import Path
from types import SimpleNamespace

from afterwatch import frames

VIDEO_ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_capture_frame_success(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        if "ffmpeg" in cmd[0]:
            Path(cmd[-1]).write_bytes(b"png")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    out = tmp_path / "shot.png"
    assert frames.capture_frame("https://youtu.be/x", 260, out) is True
    assert out.exists()


def test_capture_frame_failure_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(
        frames.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="boom", stdout=""),
    )
    out = tmp_path / "shot.png"
    assert frames.capture_frame("https://youtu.be/x", 260, out) is False


def test_capture_frame_never_raises(tmp_path, monkeypatch):
    def boom(cmd, **kwargs):
        raise OSError("ffmpeg missing")

    monkeypatch.setattr(frames.subprocess, "run", boom)
    assert frames.capture_frame("https://youtu.be/x", 5, tmp_path / "s.png") is False


def test_capture_frame_mkdir_failure_returns_false(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("cannot mkdir")
    monkeypatch.setattr(Path, "mkdir", boom)
    assert frames.capture_frame("https://youtu.be/x", 5, tmp_path / "sub" / "s.png") is False
