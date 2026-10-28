"""Fetch video metadata + captions. yt-dlp primary, youtube-transcript-api fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass

import yt_dlp


class TranscriptError(Exception):
    """No transcript could be obtained for the video."""


@dataclass
class VideoData:
    video_id: str
    title: str
    channel: str
    url: str
    transcript: str  # timestamped plain text, one "[MM:SS] text" line per cue
    caption_kind: str  # "manual" | "auto"
    language: str
    duration_seconds: int = 0


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


_TAG_RE = re.compile(r"<[^>]+>")
_CUE_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})\.\d{3}\s+-->")


def parse_vtt(vtt_text: str) -> str:
    """WebVTT -> '[MM:SS] text' lines. Strips inline tags; joins the physical
    lines of a single cue into one output line; collapses the rolling-window
    duplicate lines that auto-generated captions repeat across cues."""
    out: list[str] = []
    last_text: str | None = None
    current_ts: str | None = None
    cue_lines: list[str] = []

    def flush_cue() -> None:
        nonlocal last_text
        if not cue_lines:
            return
        joined = " ".join(cue_lines)
        last_text = joined
        out.append(f"[{current_ts or '00:00'}] {joined}")
        cue_lines.clear()

    for raw in vtt_text.splitlines():
        line = raw.strip()
        m = _CUE_TIME_RE.match(line)
        if m:
            flush_cue()
            h, mnt, s = (int(g) for g in m.groups())
            current_ts = format_timestamp(h * 3600 + mnt * 60 + s)
            continue
        if (
            not line
            or line == "WEBVTT"
            or line.startswith(("Kind:", "Language:", "NOTE", "STYLE", "REGION"))
            or line.isdigit()  # cue number
        ):
            flush_cue()
            continue
        text = _TAG_RE.sub("", line).strip()
        if not text or text == last_text:
            continue
        cue_lines.append(text)
    flush_cue()
    return "\n".join(out)


def _language_order(info: dict) -> list[str]:
    """Preference order: video's original language, then en, then de."""
    original = (info.get("language") or "").split("-")[0].lower()
    order: list[str] = []
    for lang in (original, "en", "de"):
        if lang and lang not in order:
            order.append(lang)
    return order


def _match_lang(tracks: dict, lang: str) -> list | None:
    for key, fmts in tracks.items():
        if key.split("-")[0].lower() == lang and fmts:
            return fmts
    return None


def _pick_track(info: dict) -> tuple[list, str, str] | None:
    """Best caption track as (formats, kind, language). Manual beats auto;
    within a kind, the original language beats en beats de beats anything."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    order = _language_order(info)
    for kind, tracks in (("manual", manual), ("auto", auto)):
        for lang in order:
            fmts = _match_lang(tracks, lang)
            if fmts:
                return fmts, kind, lang
        for key, fmts in tracks.items():
            if fmts:
                return fmts, kind, key.split("-")[0].lower()
    return None


def fetch_video(url: str, video_id: str) -> VideoData:
    # No cookies or sign-in, ever: Afterwatch only reads what is public.
    from .frames import YDL_JS
    opts: dict = {"skip_download": True, "quiet": True, "no_warnings": True,
                  "js_runtimes": YDL_JS}

    vtt_text = None
    kind = lang = ""
    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except yt_dlp.utils.DownloadError as exc:
                raise TranscriptError(f"video inaccessible: {exc}") from exc
            try:
                picked = _pick_track(info)
                if picked:
                    fmts, kind, lang = picked
                    fmt = next((f for f in fmts if f.get("ext") == "vtt"), None)
                    if fmt and fmt.get("url"):
                        vtt_text = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")
            except yt_dlp.utils.DownloadError as exc:
                raise TranscriptError(f"video inaccessible: {exc}") from exc
            except Exception:
                # Caption download failed after a successful extract_info (e.g. an
                # expired URL or a 429). Don't fail the whole fetch here — fall
                # through to the youtube-transcript-api fallback below.
                vtt_text = None
    except TranscriptError:
        raise
    except Exception as exc:
        raise TranscriptError(f"caption fetch failed: {exc}") from exc

    title = info.get("title") or "[Title not provided]"
    channel = info.get("uploader") or info.get("channel") or "[Channel not provided]"
    duration_seconds = int(info.get("duration") or 0)

    transcript = parse_vtt(vtt_text) if vtt_text else ""
    if not transcript.strip():
        transcript, kind, lang = _fetch_via_transcript_api(video_id)

    return VideoData(
        video_id=video_id,
        title=title,
        channel=channel,
        url=url,
        transcript=transcript,
        caption_kind=kind,
        language=lang,
        duration_seconds=duration_seconds,
    )


def _fetch_via_transcript_api(video_id: str) -> tuple[str, str, str]:
    """Fallback path: different request shape than yt-dlp, sometimes succeeds
    when yt-dlp's caption endpoint is bot-blocked. Returns (transcript, kind, lang)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        try:
            track = transcript_list.find_manually_created_transcript(["en", "de"])
            kind = "manual"
        except Exception:
            track = next(iter(transcript_list))
            kind = "auto" if getattr(track, "is_generated", True) else "manual"
        fetched = track.fetch()
        lines = [
            f"[{format_timestamp(snippet.start)}] {snippet.text.strip()}"
            for snippet in fetched
            if snippet.text.strip()
        ]
        if not lines:
            raise ValueError("transcript API returned no text")
        return "\n".join(lines), kind, getattr(fetched, "language_code", "") or ""
    except Exception as exc:
        raise TranscriptError(f"no transcript obtainable: {exc}") from exc
