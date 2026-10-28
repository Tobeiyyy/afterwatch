import re

import pytest

from afterwatch import config
from afterwatch.distill import build_addendum
from afterwatch.transcript import VideoData


def make_cfg(**notes):
    cfg = config.load()
    cfg["notes"].update(notes)
    return cfg


def video(minutes=30):
    words = " ".join(f"w{i}" for i in range(120 * minutes))
    return VideoData(url="https://youtu.be/x", video_id="x", title="T", channel="C",
                     transcript=f"[00:00] {words}", caption_kind="manual", language="en",
                     duration_seconds=minutes * 60)


def word_range(addendum):
    low, high = re.search(r"roughly \*\*(\d+)-(\d+) words", addendum).groups()
    return int(low), int(high)


def test_defaults_add_no_reader_settings_and_use_transcript_language():
    a = build_addendum([], [], video(), make_cfg())
    assert "READER SETTINGS" not in a
    assert "language of the transcript" in a


def test_language_setting_replaces_auto():
    a = build_addendum([], [], None, make_cfg(language="Spanish"))
    assert "Write the note body in Spanish" in a
    assert "language of the transcript" not in a


def test_length_scale_widens_and_narrows_the_target():
    normal = word_range(build_addendum([], [], video(), make_cfg()))
    short = word_range(build_addendum([], [], video(), make_cfg(length="short")))
    thorough = word_range(build_addendum([], [], video(), make_cfg(length="thorough")))
    assert short[1] < normal[1] < thorough[1]


def test_style_extra_instructions_and_disabled_schemas_become_rules():
    a = build_addendum([], [], None, make_cfg(style="study", extra_instructions="  Use metric units. ",
                                              disabled_schemas=["E", "Z"]))
    block = a.split("## READER SETTINGS", 1)[1]
    assert "Review questions" in block
    assert "Use metric units." in block
    assert "Schema E (Recipe Extract)" in block
    assert "Z" not in block.split("Never use Schema", 1)[1].split(".")[0]


def test_disabling_every_schema_is_refused():
    with pytest.raises(config.ConfigError):
        build_addendum([], [], None, make_cfg(disabled_schemas=list("ABCDEF")))
