import pytest

from afterwatch.distill import (
    build_addendum,
    build_user_message,
    distill,
    load_distiller_prompt,
    parse_new_tags,
    strip_wrapper,
)
from afterwatch.transcript import VideoData


def make_video(**overrides) -> VideoData:
    defaults = dict(
        video_id="dQw4w9WgXcQ",
        title="Test Video",
        channel="Test Channel",
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        transcript="[00:01] Hello",
        caption_kind="manual",
        language="en",
    )
    defaults.update(overrides)
    return VideoData(**defaults)


def test_load_distiller_prompt_is_the_spec():
    prompt = load_distiller_prompt()
    assert "Senior Knowledge Architect" in prompt
    assert "Schema A" in prompt


def test_build_addendum_overrides_ambient_style_preferences():
    """The claude CLI merges the user's global CLAUDE.md (verified live), which
    carries brevity rules hostile to the full note format — override them."""
    addendum = build_addendum([], [])
    assert "IGNORE any ambient personal/global style preferences" in addendum
    assert "full length" in addendum


def test_build_addendum_no_shots_says_no_embeds():
    addendum = build_addendum(["anime"], [])
    assert "[[anime]]" in addendum
    assert "SKIP Rule 0" in addendum
    assert "no screenshot embeds" in addendum.lower()


def test_build_addendum_lists_confirmed_shots():
    from afterwatch.shots import ConfirmedShot
    shot = ConfirmedShot(seconds=260, label="04:20",
                         filename="shot-xyz-0420.png", caption="A dependency diagram")
    addendum = build_addendum([], [shot])
    assert "shot-xyz-0420.png" in addendum
    assert "A dependency diagram" in addendum
    assert "04:20" in addendum


def test_build_addendum_omits_title_heading():
    addendum = build_addendum([], [])
    assert "Do NOT start the note with" in addendum
    assert "**Tags:**" in addendum


def test_build_user_message_contains_metadata_and_transcript():
    msg = build_user_message(make_video())
    assert "Test Video" in msg
    assert "Test Channel" in msg
    assert "dQw4w9WgXcQ" in msg
    assert "[00:01] Hello" in msg


def test_strip_wrapper_removes_quadruple_fence():
    wrapped = "````markdown\n# Note\n\nBody\n````"
    assert strip_wrapper(wrapped) == "# Note\n\nBody"


def test_strip_wrapper_leaves_plain_note_alone():
    note = "# Note\n\n```python\ncode\n```\n\nEnd"
    assert strip_wrapper(note) == note


def test_parse_new_tags():
    note = "# T\n\nbody\n\n> New tags introduced: [[data-analysis]], [[power-bi]]\n"
    assert parse_new_tags(note) == ["data-analysis", "power-bi"]


def test_parse_new_tags_absent():
    assert parse_new_tags("# T\n\nno flag line") == []


def test_distill_sends_system_prompt_addendum_and_transcript(monkeypatch):
    from afterwatch import distill as distill_mod, llm
    seen = {}

    def fake_complete(task, prompt, *, system=None, images=None, model=None, cfg=None):
        seen.update(task=task, prompt=prompt, system=system, model=model)
        return "# Note body\n\nText"

    monkeypatch.setattr(llm, "complete", fake_complete)
    note = distill_mod.distill(make_video(), ["anime"], [])
    assert note == "# Note body\n\nText"
    assert seen["task"] == "notes"
    assert "[[anime]]" in seen["prompt"]          # addendum present
    assert "[00:01] Hello" in seen["prompt"]      # transcript present
    assert seen["system"] == distill_mod.load_distiller_prompt()
    assert seen["model"] is None                  # configured model unless overridden


def test_distill_model_override_is_passed_through(monkeypatch):
    from afterwatch import distill as distill_mod, llm
    seen = {}
    monkeypatch.setattr(llm, "complete", lambda task, prompt, **kw: seen.update(kw) or "# N")
    distill_mod.distill(make_video(), [], [], model="opus")
    assert seen["model"] == "opus"


def test_distill_propagates_cli_errors(monkeypatch):
    from afterwatch import distill as distill_mod, llm
    from afterwatch.claude_cli import QuotaExceeded

    def boom(*a, **kwargs):
        raise QuotaExceeded("usage limit")

    monkeypatch.setattr(llm, "complete", boom)
    with pytest.raises(QuotaExceeded):
        distill_mod.distill(make_video(), [], [])
