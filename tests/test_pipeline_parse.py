import pytest

from afterwatch.pipeline import (
    LinkParseError,
    LinkRequest,
    extract_video_id,
    parse_line,
)


def test_extract_from_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_from_short_url_with_playlist_params():
    url = "https://youtu.be/dQw4w9WgXcQ?list=PLabc123xyz&index=3"
    assert extract_video_id(url) == "dQw4w9WgXcQ"


def test_extract_from_watch_url_with_leading_params():
    url = "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"
    assert extract_video_id(url) == "dQw4w9WgXcQ"


def test_extract_from_shorts_and_live_urls():
    assert extract_video_id("https://www.youtube.com/shorts/abc123DEF-_") == "abc123DEF-_"
    assert extract_video_id("https://www.youtube.com/live/abc123DEF-_") == "abc123DEF-_"


def test_extract_rejects_non_video_url():
    with pytest.raises(LinkParseError):
        extract_video_id("https://www.youtube.com/@somechannel")


def test_parse_line_canonicalizes_and_strips_playlist():
    req = parse_line("https://youtu.be/dQw4w9WgXcQ?list=PLabc&index=2")
    assert req == LinkRequest(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        video_id="dQw4w9WgXcQ",
    )


def test_parse_line_shots_token_is_stripped_but_ignored():
    req = parse_line("https://youtu.be/dQw4w9WgXcQ +shots")
    assert req == LinkRequest(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                              video_id="dQw4w9WgXcQ")


def test_parse_line_blank_and_comment_return_none():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("# a comment") is None


def test_parse_line_garbage_raises():
    with pytest.raises(LinkParseError):
        parse_line("not a url at all")
