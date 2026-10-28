import datetime as dt

import pytest

from afterwatch.writer import (
    append_failure,
    create_tag_stubs,
    find_existing_note,
    sanitize_filename,
    write_note,
)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Videos").mkdir(parents=True)
    (tmp_path / "Tags").mkdir()
    return tmp_path


def test_sanitize_filename_strips_windows_and_obsidian_chars():
    assert sanitize_filename('A/B: "C" <D>? #E [F] | G*') == "AB C D E F  G"
    assert sanitize_filename("  Normal Title  ") == "Normal Title"
    assert sanitize_filename("???") == "Untitled"


def test_write_note_appends_references_iframe(vault):
    path = write_note(vault, "My Video", "dQw4w9WgXcQ", "# My Video\n\nBody")
    content = path.read_text(encoding="utf-8")
    assert path.name == "My Video.md"
    assert content.startswith("Body")  # leading H1 stripped: filename is the title
    assert "# References" in content
    assert "youtube.com/embed/dQw4w9WgXcQ" in content


def test_write_note_strips_leading_title_heading_only(vault):
    note = "# Some Title\n\n**Tags:** [[youtube]]\n\n## Section\n\nText"
    path = write_note(vault, "Some Title", "AAAAAAAAAAA", note)
    content = path.read_text(encoding="utf-8")
    assert content.startswith("**Tags:** [[youtube]]")
    assert "## Section" in content  # only the FIRST leading H1 goes


def test_write_note_keeps_note_without_heading(vault):
    path = write_note(vault, "T", "BBBBBBBBBBB", "**Tags:** [[youtube]]\n\nBody")
    assert path.read_text(encoding="utf-8").startswith("**Tags:** [[youtube]]")


def test_write_note_title_collision_gets_id_suffix(vault):
    write_note(vault, "Same Title", "AAAAAAAAAAA", "# a")
    path = write_note(vault, "Same Title", "BBBBBBBBBBB", "# b")
    assert path.name == "Same Title (BBBBBBBBBBB).md"


def test_find_existing_note_by_video_id(vault):
    written = write_note(vault, "My Video", "dQw4w9WgXcQ", "# note")
    assert find_existing_note(vault, "dQw4w9WgXcQ") == written
    assert find_existing_note(vault, "AAAAAAAAAAA") is None


def test_create_tag_stubs_skips_existing(vault):
    (vault / "Tags" / "anime.md").write_text("", encoding="utf-8")
    created = create_tag_stubs(vault, ["anime", "new-tag"])
    assert created == ["new-tag"]
    assert (vault / "Tags" / "new-tag.md").exists()


def test_append_failure_accumulates_dated_entries(vault):
    append_failure(vault, "https://youtu.be/aaa", "no transcript")
    append_failure(vault, "https://youtu.be/bbb", "private video")
    text = (
        vault / "Afterwatch - Failed videos.md"
    ).read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    assert f"- [{today}] https://youtu.be/aaa — no transcript" in text
    assert f"- [{today}] https://youtu.be/bbb — private video" in text
    assert text.startswith("# Afterwatch — Failed videos")
