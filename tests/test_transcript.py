import pytest
import yt_dlp

from afterwatch.transcript import (
    TranscriptError,
    VideoData,
    _language_order,
    _pick_track,
    fetch_video,
    format_timestamp,
    parse_vtt,
)

SAMPLE_VTT = """\
WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
Hello <c>world</c>

00:00:03.000 --> 00:00:05.000
Hello world

00:01:15.500 --> 00:01:18.000
Second line

01:02:03.000 --> 01:02:05.000
Past the hour
"""


def test_format_timestamp():
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(75.9) == "01:15"
    assert format_timestamp(3723) == "1:02:03"


def test_parse_vtt_strips_tags_and_dedupes():
    result = parse_vtt(SAMPLE_VTT)
    assert result == "[00:01] Hello world\n[01:15] Second line\n[1:02:03] Past the hour"


def test_parse_vtt_empty_input():
    assert parse_vtt("WEBVTT\n\n") == ""


def test_parse_vtt_joins_multiline_cue():
    vtt = """\
WEBVTT

00:00:01.000 --> 00:00:03.000
This is a longer sentence that
wraps onto a second physical line.
"""
    result = parse_vtt(vtt)
    assert result == "[00:01] This is a longer sentence that wraps onto a second physical line."


def test_parse_vtt_rolling_window_dedupe():
    vtt = """\
WEBVTT

00:00:00.000 --> 00:00:02.000
Hello world

00:00:02.000 --> 00:00:04.000
Hello world
next words
"""
    result = parse_vtt(vtt)
    assert result == "[00:00] Hello world\n[00:02] next words"


def test_language_order_prefers_original_then_en_de():
    assert _language_order({"language": "de"}) == ["de", "en"]
    assert _language_order({"language": "en-US"}) == ["en", "de"]
    assert _language_order({}) == ["en", "de"]


def test_pick_track_prefers_manual_over_auto():
    info = {
        "language": "en",
        "subtitles": {"en": [{"ext": "vtt", "url": "http://x/manual"}]},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "http://x/auto"}]},
    }
    fmts, kind, lang = _pick_track(info)
    assert kind == "manual"
    assert fmts[0]["url"] == "http://x/manual"


def test_pick_track_falls_back_to_auto_in_original_language():
    info = {
        "language": "de",
        "subtitles": {},
        "automatic_captions": {
            "de-orig": [{"ext": "vtt", "url": "http://x/de-auto"}],
            "en": [{"ext": "vtt", "url": "http://x/en-auto"}],
        },
    }
    fmts, kind, lang = _pick_track(info)
    assert kind == "auto"
    assert lang == "de"
    assert fmts[0]["url"] == "http://x/de-auto"


def test_pick_track_none_when_no_captions():
    assert _pick_track({"subtitles": {}, "automatic_captions": {}}) is None


class _FakeYoutubeDLBase:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_fetch_video_download_error_becomes_transcript_error(monkeypatch):
    class FakeYoutubeDL(_FakeYoutubeDLBase):
        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("Private video")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)

    with pytest.raises(TranscriptError):
        fetch_video("https://www.youtube.com/watch?v=abc123", "abc123")


def test_fetch_video_caption_download_failure_falls_back(monkeypatch):
    info = {
        "title": "A Video",
        "uploader": "A Channel",
        "language": "en",
        "subtitles": {},
        "automatic_captions": {"en": [{"ext": "vtt", "url": "http://x/en-auto"}]},
    }

    class FakeYoutubeDL(_FakeYoutubeDLBase):
        def extract_info(self, url, download=False):
            return info

        def urlopen(self, url):
            raise Exception("HTTP 429")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        "afterwatch.transcript._fetch_via_transcript_api",
        lambda video_id: ("[00:01] fallback", "auto", "en"),
    )

    result = fetch_video("https://www.youtube.com/watch?v=abc123", "abc123")

    assert isinstance(result, VideoData)
    assert result.transcript == "[00:01] fallback"
    assert result.caption_kind == "auto"
    assert result.language == "en"


def test_video_data_has_duration_field():
    from afterwatch.transcript import VideoData
    v = VideoData(
        video_id="x", title="t", channel="c", url="u",
        transcript="", caption_kind="auto", language="en", duration_seconds=213,
    )
    assert v.duration_seconds == 213
