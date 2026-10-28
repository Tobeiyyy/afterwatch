"""Frame capture: a 2-second yt-dlp segment download, then one ffmpeg frame."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SEGMENT_SECONDS = 2
SUBPROCESS_TIMEOUT = 300  # per command, seconds
# yt-dlp enables only Deno by default; YouTube's JS challenges need one of these,
# so allow whichever the user has installed.
JS_RUNTIMES = ("deno", "node", "quickjs")
YDL_JS = {r: {} for r in JS_RUNTIMES}
CLI_JS = [arg for r in JS_RUNTIMES for arg in ("--js-runtimes", r)]


def _ytdlp_cmd(url: str, start: int, out_path: Path) -> list[str]:
    return [
        sys.executable, "-m", "yt_dlp", *CLI_JS,
        "-f", "bv*[height<=1080]/bv*/b",
        "--download-sections", f"*{start}-{start + SEGMENT_SECONDS}",
        "--force-keyframes-at-cuts",  # re-encodes the cut so it starts exactly at `start`
        "--remux-video", "mp4",
        "--quiet", "--no-warnings",
        "-o", str(out_path),
        url,
    ]


def _ffmpeg_cmd(segment: Path, png: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(segment),
        "-frames:v", "1",
        # 1280 px JPG keeps slide text sharp at ~1/7 of a full-res PNG; the
        # vault syncs through Obsidian Sync's 1 GB limit.
        "-vf", "scale='min(1280,iw)':-2", "-q:v", "4",
        str(png),
    ]


def capture_frame(url: str, seconds: int, out_path: Path) -> bool:
    """Capture one full-resolution frame at `seconds` into out_path.
    Non-fatal: returns False on any failure instead of raising."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="afterwatch-shot-") as tmp:
            segment = Path(tmp) / "seg.mp4"
            for cmd in (_ytdlp_cmd(url, seconds, segment), _ffmpeg_cmd(segment, out_path)):
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
                )
                if proc.returncode != 0:
                    return False
            return out_path.exists()
    except Exception:
        return False
