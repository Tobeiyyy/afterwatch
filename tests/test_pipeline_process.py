from pathlib import Path

import pytest

from afterwatch.pipeline import LinkRequest, process_one
from afterwatch.transcript import TranscriptError, VideoData

VIDEO_ID = "dQw4w9WgXcQ"
REQ = LinkRequest(
    url=f"https://www.youtube.com/watch?v={VIDEO_ID}", video_id=VIDEO_ID
)
FAKE_NOTE = (
    "# Test Video\n\n**Tags:** [[youtube]], [[new-tag]]\n\nBody\n\n"
    "> New tags introduced: [[new-tag]]\n"
)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Videos").mkdir(parents=True)
    (tmp_path / "Tags").mkdir()
    return tmp_path


def fake_video() -> VideoData:
    return VideoData(
        video_id=VIDEO_ID,
        title="Test Video",
        channel="Chan",
        url=REQ.url,
        transcript="[00:01] hi",
        caption_kind="manual",
        language="en",
    )


def test_happy_path_creates_note_and_tag_stub(vault, monkeypatch):
    monkeypatch.setattr(
        "afterwatch.transcript.fetch_video", lambda url, vid, **kw: fake_video()
    )
    monkeypatch.setattr("afterwatch.shots.plan_shots", lambda video, vault, **kw: [])
    monkeypatch.setattr(
        "afterwatch.distill.distill", lambda video, tags, confirmed_shots, **kw: FAKE_NOTE
    )
    result = process_one(REQ, vault)
    assert result.status == "created"
    assert result.note_path.exists()
    assert "# References" in result.note_path.read_text(encoding="utf-8")
    assert result.new_tags == ["new-tag"]
    assert (vault / "Tags" / "new-tag.md").exists()


def test_duplicate_is_skipped_without_fetching(vault, monkeypatch):
    existing = vault / "Videos" / "Old Note.md"
    existing.write_text(f"src: https://youtu.be/{VIDEO_ID}", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("fetch_video must not be called for duplicates")

    monkeypatch.setattr("afterwatch.transcript.fetch_video", boom)
    result = process_one(REQ, vault)
    assert result.status == "duplicate"
    assert result.note_path == existing


def test_transcript_failure_is_logged_not_raised(vault, monkeypatch):
    def fail(url, vid, **kw):
        raise TranscriptError("no transcript obtainable")

    monkeypatch.setattr("afterwatch.transcript.fetch_video", fail)
    result = process_one(REQ, vault)
    assert result.status == "failed"
    assert "no transcript" in result.reason
    failure_note = vault / "Afterwatch - Failed videos.md"
    assert REQ.url in failure_note.read_text(encoding="utf-8")


def test_creates_note_with_confirmed_shots(vault, monkeypatch):
    from afterwatch.shots import ConfirmedShot
    monkeypatch.setattr("afterwatch.transcript.fetch_video", lambda url, vid, **kw: fake_video())
    shot = ConfirmedShot(260, "04:20", "shot-dQw4w9WgXcQ-0420.png", "a diagram")
    monkeypatch.setattr("afterwatch.shots.plan_shots", lambda video, vault, **kw: [shot])
    captured = {}
    def fake_distill(video, tags, confirmed_shots, **kw):
        captured["shots"] = confirmed_shots
        return FAKE_NOTE
    monkeypatch.setattr("afterwatch.distill.distill", fake_distill)
    result = process_one(REQ, vault)
    assert result.status == "created"
    assert captured["shots"] == [shot]


def test_talking_head_gets_no_shots(vault, monkeypatch):
    monkeypatch.setattr("afterwatch.transcript.fetch_video", lambda url, vid, **kw: fake_video())
    monkeypatch.setattr("afterwatch.shots.plan_shots", lambda video, vault, **kw: [])
    seen = {}
    monkeypatch.setattr("afterwatch.distill.distill",
                        lambda video, tags, confirmed_shots, **kw: seen.update(s=confirmed_shots) or FAKE_NOTE)
    result = process_one(REQ, vault)
    assert result.status == "created" and seen["s"] == []
