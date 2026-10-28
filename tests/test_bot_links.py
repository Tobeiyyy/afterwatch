from pathlib import Path
from types import SimpleNamespace

from afterwatch.bot_links import (
    extract_requests,
    format_reply,
    is_handled,
    outcome_reactions,
)
from afterwatch.pipeline import Result, find_video_ids

VID_A = "dQw4w9WgXcQ"
VID_B = "abc123DEF-_"


def test_find_video_ids_in_free_text():
    text = f"look https://youtu.be/{VID_A}?si=xyz and https://www.youtube.com/watch?v={VID_B} again https://youtu.be/{VID_A}"
    assert find_video_ids(text) == [VID_A, VID_B]


def test_find_video_ids_none():
    assert find_video_ids("just chatting, no links") == []


def test_extract_requests_strips_shots_token():
    text = f"https://youtu.be/{VID_A} +shots https://youtu.be/{VID_B}"
    requests = extract_requests(text)
    assert [r.video_id for r in requests] == [VID_A, VID_B]
    assert requests[0].url == f"https://www.youtube.com/watch?v={VID_A}"


def test_extract_requests_normal_without_shots_token():
    requests = extract_requests(f"https://youtu.be/{VID_A}")
    assert requests[0].video_id == VID_A


def test_is_handled():
    handled = SimpleNamespace(
        reactions=[
            SimpleNamespace(me=False, emoji="✅"),
            SimpleNamespace(me=True, emoji="✅"),
        ]
    )
    fresh = SimpleNamespace(reactions=[SimpleNamespace(me=False, emoji="✅")])
    empty = SimpleNamespace(reactions=[])
    assert is_handled(handled) is True
    assert is_handled(fresh) is False
    assert is_handled(empty) is False


def test_is_handled_wait_only_not_handled():
    """A bot ⏳ reaction alone must not count as handled: after a crash, the
    message should still be reprocessed on catch-up."""
    wait_only = SimpleNamespace(reactions=[SimpleNamespace(me=True, emoji="⏳")])
    assert is_handled(wait_only) is False


def test_is_handled_non_bot_terminal_emoji_not_handled():
    non_bot = SimpleNamespace(reactions=[SimpleNamespace(me=False, emoji="✅")])
    assert is_handled(non_bot) is False


def test_is_handled_reaction_missing_emoji_attr_not_handled():
    missing_emoji = SimpleNamespace(reactions=[SimpleNamespace(me=True)])
    assert is_handled(missing_emoji) is False


def test_is_handled_duplicate_and_failed_terminal_emojis():
    dup = SimpleNamespace(reactions=[SimpleNamespace(me=True, emoji="♻️")])
    failed = SimpleNamespace(reactions=[SimpleNamespace(me=True, emoji="❌")])
    assert is_handled(dup) is True
    assert is_handled(failed) is True


def test_format_reply_created_with_extras():
    result = Result(
        url="u", status="created", video_id=VID_A,
        note_path=Path("My Note.md"), new_tags=["new-tag"],
        shot_failures=["shot-x-0420.png: boom"],
    )
    text = format_reply(result)
    assert "created: My Note.md" in text
    assert "new tags: new-tag" in text
    assert "frame capture failed" in text and "boom" in text


def test_format_reply_duplicate_and_failed():
    dup = Result(url="u", status="duplicate", video_id=VID_A, note_path=Path("Old.md"))
    failed = Result(url="u", status="failed", video_id=VID_A, reason="no transcript obtainable")
    assert "already in vault: Old.md" in format_reply(dup)
    assert "failed: no transcript obtainable" in format_reply(failed)


def test_outcome_reactions_mixed():
    created = Result(url="u", status="created", video_id=VID_A, note_path=Path("N.md"))
    dup = Result(url="u", status="duplicate", video_id=VID_A, note_path=Path("N.md"))
    failed = Result(url="u", status="failed", video_id=VID_A, reason="x")
    assert outcome_reactions([created]) == ["✅"]
    assert outcome_reactions([dup]) == ["♻️"]
    assert outcome_reactions([failed]) == ["❌"]
    assert outcome_reactions([created, dup, failed]) == ["✅", "♻️", "❌"]
