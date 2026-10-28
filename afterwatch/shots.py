"""Smart screenshots: storyboard-based locate + full-res confirm/caption."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp

from . import llm
from .frames import CLI_JS, SUBPROCESS_TIMEOUT, YDL_JS, capture_frame
from . import config

MAX_SHOTS = 3  # default for screenshots.max_per_note
_SAMPLE_TILES = 12  # ffmpeg-fallback contact-sheet tile count


def format_time_label(seconds: int) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def shot_filename(video_id: str, seconds: int) -> str:
    label = format_time_label(seconds).replace(":", "")  # 04:20->0420, 1:02:03->10203
    return f"shot-{video_id}-{label}.jpg"


@dataclass
class Overview:
    image_path: Path
    tile_seconds: list[int] = field(default_factory=list)


def best_storyboard(info: dict) -> dict | None:
    sbs = [f for f in info.get("formats", []) if f.get("format_note") == "storyboard"]
    sbs = [f for f in sbs if f.get("fragments") and f.get("rows") and f.get("columns")]
    if not sbs:
        return None
    # Whole video in one image: fewest fragments, then most tiles per sprite.
    return min(sbs, key=lambda f: (len(f["fragments"]), -(f["rows"] * f["columns"])))


def tile_timestamps(sb: dict) -> list[int]:
    """Per-tile start seconds for the sprite's FIRST fragment, row-major."""
    tiles = sb["rows"] * sb["columns"]
    dur = float(sb["fragments"][0].get("duration") or 0.0)
    per = (dur / tiles) if tiles else 0.0
    return [int(i * per) for i in range(tiles)]


def _extract_info(url: str) -> dict:
    opts = {"skip_download": True, "quiet": True, "no_warnings": True, "js_runtimes": YDL_JS}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _ffmpeg_contact_sheet(url, duration_seconds, out_path, workdir) -> list[int] | None:
    """Fallback: sample frames evenly by TIME and tile them into one image via
    ffmpeg. Sampling one frame every `step` seconds (rather than every N frames)
    keeps the returned seconds in sync with the tiles regardless of the source
    video's fps. Returns per-tile seconds, or None on failure."""
    n = _SAMPLE_TILES
    step = max(1, duration_seconds // n)
    seconds = [i * step for i in range(n)]
    try:
        # Download a small (low-res) copy for sampling to avoid a full-quality pull.
        tmp = Path(workdir) / "sample.mp4"
        dl = [
            sys.executable, "-m", "yt_dlp", *CLI_JS,
            "-f", "bv*[height<=360]/worst", "--quiet", "--no-warnings",
            "-o", str(tmp), url,
        ]
        if subprocess.run(dl, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT).returncode != 0:
            return None
        cols = 4
        rows = (n + cols - 1) // cols
        vf = f"fps=1/{step},scale=320:-1,tile={cols}x{rows}"
        sheet = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp),
                 "-vf", vf, "-frames:v", "1", str(out_path)]
        if subprocess.run(sheet, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT).returncode != 0:
            return None
        return seconds if out_path.exists() else None
    except Exception:
        return None


def build_overview(video_id, url, duration_seconds, workdir) -> Overview | None:
    """One whole-video overview image + each tile's timestamp. None if unavailable."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / f"overview-{video_id}.jpg"
    try:
        info = _extract_info(url)
    except Exception:
        info = {"formats": []}
    sb = best_storyboard(info)
    if sb:
        try:
            data = _fetch_bytes(sb["fragments"][0]["url"])
            out.write_bytes(data)
            return Overview(image_path=out, tile_seconds=tile_timestamps(sb))
        except Exception:
            pass
    secs = _ffmpeg_contact_sheet(url, duration_seconds, out, workdir)
    if secs is not None and out.exists():
        return Overview(image_path=out, tile_seconds=secs)
    return None


@dataclass
class ConfirmedShot:
    seconds: int
    label: str
    filename: str
    caption: str


_LOCATE_PROMPT = (
    "This is a contact sheet of thumbnails sampled across a YouTube video, in "
    "chronological order (left-to-right, top-to-bottom). Tile timestamps (seconds): "
    "{timestamps}.\n\n"
    "Your job is to find tiles that show a DELIBERATE INFORMATIONAL GRAPHIC the viewer "
    "would want to re-read later as a standalone image: a diagram, chart/graph, slide "
    "with legible text, code or terminal, whiteboard/handwriting, a labeled figure, or "
    "a clear demonstration of the specific object being discussed.\n\n"
    "Default to NOT visual. Most videos — talking-head explainers, vlogs, podcasts, "
    "interviews, reaction videos — contain NO such graphics and must return "
    '"visual": false with an empty candidate list. A person talking, gesturing, or '
    "reacting, a webcam/studio shot, and a plain or decorative background are NOT "
    "informational graphics, no matter what is being said. When in doubt, exclude the "
    "tile.\n\nTranscript context (for topic only, NOT evidence of a visual):\n{excerpt}\n\n"
    'Return ONLY compact JSON: {{"visual": true/false, "candidates": [seconds, ...]}} '
    "with at most 5 candidate timestamps whose tiles clearly contain such a graphic. "
    "If none do, return visual=false and candidates=[]."
)

_CONFIRM_PROMPT = (
    "Each image below is a full-resolution frame from a video, labeled with its "
    "timestamp in seconds. This is the final, strict gate before a frame is saved into "
    "a personal knowledge vault.\n\n"
    "Mark a frame useful=true ONLY if ALL of these hold: (1) it clearly shows a "
    "deliberate informational graphic — a diagram, chart/graph, slide with legible "
    "text, code/terminal, whiteboard/handwriting, labeled figure, or a clear "
    "demonstration of the specific object under discussion; (2) it is sharp and "
    "legible — NOT blurry, motion-blurred, or a mid-movement/transition frame; (3) it "
    "would still be worth looking at on its own weeks later.\n\n"
    "Mark useful=false for anything else, including: a frame that is primarily a person "
    "(talking, gesturing, reacting), a webcam/studio shot, a plain/decorative "
    "background, or a frame whose only 'content' is the presenter. It is normal and "
    "CORRECT for a video to yield zero useful frames — returning all useful=false is "
    "the right answer for a talking-head video. Do not feel obligated to keep any. When "
    "uncertain, mark useful=false.\n\n"
    "For useful frames, write a one-line caption describing ONLY what is actually "
    "visible in that frame (never from the transcript). Transcript context (topic "
    "only):\n{excerpt}\n\n"
    'Return ONLY compact JSON: {{"shots": [{{"seconds": N, "useful": true/false, '
    '"caption": "..."}}, ...]}}.'
)


# "always" mode: no "is this video visual at all" gate; the model must pick the
# most informative tiles, and the confirm step only drops unreadable frames.
_LOCATE_ALWAYS_PROMPT = (
    "This is a contact sheet of thumbnails sampled across a YouTube video, in "
    "chronological order (left-to-right, top-to-bottom). Tile timestamps (seconds): "
    "{timestamps}.\n\n"
    "Pick the tiles that would be most worth keeping as screenshots in notes about this "
    "video: informational graphics first (diagrams, charts, slides, code, labeled "
    "figures, the object being discussed), otherwise the clearest, most representative "
    "moments.\n\nTranscript context:\n{excerpt}\n\n"
    'Return ONLY compact JSON: {{"visual": true, "candidates": [seconds, ...]}} with up '
    "to 5 candidate timestamps."
)

_CONFIRM_ALWAYS_PROMPT = (
    "Each image below is a full-resolution frame from a video, labeled with its "
    "timestamp in seconds. Mark a frame useful=false only if it is blurry, a "
    "mid-transition frame, or blank; otherwise useful=true. For every useful frame, "
    "write a one-line caption describing ONLY what is visible in it (never from the "
    "transcript). Transcript context (topic only):\n{excerpt}\n\n"
    'Return ONLY compact JSON: {{"shots": [{{"seconds": N, "useful": true/false, '
    '"caption": "..."}}, ...]}}.'
)


def screenshot_settings(cfg: dict | None = None) -> tuple[str, int]:
    """(mode, max_per_note). Mode is forced to "off" when no vision model is set
    or its provider can't read images."""
    cfg = cfg or config.load()
    s = cfg["screenshots"]
    mode, limit = s.get("mode", "auto"), int(s.get("max_per_note", MAX_SHOTS))
    try:
        provider, _ = config.model_for("vision", cfg)
    except config.ConfigError:
        return "off", limit
    if not cfg["providers"][provider].get("vision") or limit <= 0:
        return "off", limit
    return mode, limit


def _vision_call(model, images: list[tuple[str, bytes]], prompt: str) -> str:
    """images: list of (label, jpeg_or_png_bytes). Returns the model's text.
    ``model`` None = the model configured for the "vision" task."""
    return llm.complete("vision", prompt, images=images or None, model=model)

def _parse_json(text: str) -> dict:
    try:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return {}
        return json.loads(text[start : end + 1])
    except Exception:
        return {}


def locate(overview: Overview, transcript_excerpt: str, model: str,
           mode: str = "auto") -> list[int]:
    try:
        data = overview.image_path.read_bytes()
    except OSError:
        data = None
    template = _LOCATE_ALWAYS_PROMPT if mode == "always" else _LOCATE_PROMPT
    prompt = template.format(
        timestamps=overview.tile_seconds, excerpt=transcript_excerpt[:2000]
    )
    images = [("contact sheet:", data)] if data is not None else []
    parsed = _parse_json(_vision_call(model, images, prompt))
    if not parsed.get("visual"):
        return []
    out = []
    for c in parsed.get("candidates", []):
        try:
            out.append(int(c))
        except (TypeError, ValueError):
            continue
    return out[:5]


def confirm(candidates_with_paths, transcript_excerpt: str, model: str,
            mode: str = "auto", limit: int = MAX_SHOTS) -> dict[int, str]:
    if not candidates_with_paths:
        return {}
    images = []
    for seconds, path in candidates_with_paths:
        try:
            images.append((f"timestamp {seconds}s:", Path(path).read_bytes()))
        except OSError:
            continue
    template = _CONFIRM_ALWAYS_PROMPT if mode == "always" else _CONFIRM_PROMPT
    prompt = template.format(excerpt=transcript_excerpt[:2000])
    parsed = _parse_json(_vision_call(model, images, prompt))
    result: dict[int, str] = {}
    for shot in parsed.get("shots", []):
        try:
            if shot.get("useful") and shot.get("caption"):
                result[int(shot["seconds"])] = str(shot["caption"]).strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if len(result) >= limit:
            break
    return result


def plan_shots(video, vault, model: str | None = None,
               cfg: dict | None = None) -> list[ConfirmedShot]:
    """Locate -> capture full-res -> confirm+caption. [] when screenshots are off,
    on non-visual videos, or on any non-fatal failure. Never raises."""
    try:
        mode, limit = screenshot_settings(cfg)
        if mode == "off":
            return []
        with tempfile.TemporaryDirectory(prefix="afterwatch-shots-") as tmp:
            overview = build_overview(video.video_id, video.url, video.duration_seconds, Path(tmp))
            if overview is None:
                return []
            candidates = locate(overview, video.transcript, model, mode)
            if not candidates:
                return []
            images_dir = Path(vault) / config.folder("images")
            images_dir.mkdir(parents=True, exist_ok=True)
            captured: list[tuple[int, Path]] = []
            for seconds in candidates[:5]:
                path = images_dir / shot_filename(video.video_id, seconds)
                if capture_frame(video.url, seconds, path):
                    captured.append((seconds, path))
            if not captured:
                return []
            captions = confirm(captured, video.transcript, model, mode, limit)
            shots_out: list[ConfirmedShot] = []
            for seconds, path in captured:
                caption = captions.get(seconds)
                if caption:
                    shots_out.append(ConfirmedShot(
                        seconds=seconds, label=format_time_label(seconds),
                        filename=path.name, caption=caption,
                    ))
                else:
                    path.unlink(missing_ok=True)  # rejected frame: don't leave it in the vault
            return shots_out[:limit]
    except Exception:
        return []
