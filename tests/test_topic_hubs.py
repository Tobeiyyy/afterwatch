from pathlib import Path

import pytest

from afterwatch import resurface as R
from afterwatch import topic_hubs as T
from afterwatch.hubs import END, START


def note(i, tags):
    return R.Note(path=Path(f"N{i}.md"), title=f"Note {i}", tags=list(tags),
                  video_id=f"vid{i:08d}", body=f"body {i}")


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Hubs").mkdir(parents=True)
    return tmp_path


def fake_ask(themes_of=None, assign=None):
    """Stands in for Claude. Split requests get `themes_of(numbers)` (default:
    chunks of 10); assignment requests get `assign(n_new)`."""
    calls = []

    def ask(prompt):
        calls.append(prompt)
        numbers = [int(line.split(".")[0]) for line in prompt.splitlines()
                   if line[:1].isdigit() and "." in line[:5]]
        if "into themes" in prompt:
            groups = (themes_of or (lambda ns: [ns[i:i + 10] for i in range(0, len(ns), 10)]))(numbers)
            return {"themes": [{"name": f"Theme {k}", "notes": g}
                                for k, g in enumerate(groups, 1)]}
        return {"assign": (assign or (lambda ns: {str(n): "NEW" for n in ns}))(numbers)}

    ask.calls = calls
    return ask


# --- topics ---

def test_topic_is_the_rarest_tag_shared_by_at_least_five_notes():
    counts = {"video-game": 500, "pokemon": 40, "rare": 2}
    assert T.topic_of(note(1, ["video-game", "pokemon", "rare"]), counts) == "pokemon"
    assert T.topic_of(note(2, ["rare"]), counts) is None  # below the threshold


def test_format_tags_never_become_a_topic():
    notes = [note(i, ["video-essay"]) for i in range(30)]
    assert all(T.topic_of(n, T.tag_counts(notes)) is None for n in notes)


# --- planning a fresh vault ---

def test_small_topic_becomes_one_hub_named_after_it(vault):
    notes = [note(i, ["pokemon"]) for i in range(7)]
    m = T.plan(vault, notes, {}, fake_ask())
    assert list(m["hubs"]) == ["pokemon Hub"]
    assert len(m["hubs"]["pokemon Hub"]["members"]) == 7


def test_topic_under_five_notes_gets_no_hub_yet(vault):
    assert T.plan(vault, [note(i, ["pokemon"]) for i in range(4)], {}, fake_ask())["hubs"] == {}


def test_big_topic_is_split_by_claude_into_capped_themes(vault):
    notes = [note(i, ["video-game"]) for i in range(40)]
    m = T.plan(vault, notes, {}, fake_ask())
    sizes = sorted(len(h["members"]) for h in m["hubs"].values())
    assert sizes == [10, 10, 10, 10]
    assert all(name.startswith("video-game - ") for name in m["hubs"])


def test_theme_split_never_loses_or_duplicates_a_note(vault):
    """Claude skips some numbers, repeats one and returns a theme of 2: every
    note still ends up in exactly one hub, and none over the cap."""
    def sloppy(ns):
        return [ns[0:12] + [ns[0]], ns[12:14], ns[20:31]]  # 14..19 and 31.. missing
    notes = [note(i, ["video-game"]) for i in range(40)]
    m = T.plan(vault, notes, {}, fake_ask(themes_of=sloppy))
    members = [v for h in m["hubs"].values() for v in h["members"]]
    assert sorted(members) == sorted(n.video_id for n in notes)
    assert all(T.MIN_NOTES <= len(h["members"]) <= T.MAX_NOTES for h in m["hubs"].values())


# --- updating ---

def test_new_notes_join_the_hub_claude_picks(vault):
    old = [note(i, ["pokemon"]) for i in range(6)]
    m = T.plan(vault, old, {}, fake_ask())
    T.mark_written(m, "pokemon Hub")
    new = [note(i, ["pokemon"]) for i in range(6, 9)]
    m = T.plan(vault, old + new, m, fake_ask(assign=lambda ns: {str(n): "pokemon Hub" for n in ns}))
    assert len(m["hubs"]["pokemon Hub"]["members"]) == 9
    assert T.needs_writing(m) == ["pokemon Hub"]  # 3 new notes -> rewrite


def test_two_new_notes_are_not_enough_for_a_rewrite(vault):
    old = [note(i, ["pokemon"]) for i in range(6)]
    m = T.plan(vault, old, {}, fake_ask())
    T.mark_written(m, "pokemon Hub")
    new = [note(i, ["pokemon"]) for i in range(6, 8)]
    m = T.plan(vault, old + new, m, fake_ask(assign=lambda ns: {str(n): "pokemon Hub" for n in ns}))
    assert T.needs_writing(m) == []


def test_hub_growing_past_the_cap_is_split_and_the_old_one_superseded(vault):
    old = [note(i, ["pokemon"]) for i in range(14)]
    m = T.plan(vault, old, {}, fake_ask())
    T.mark_written(m, "pokemon Hub")
    new = [note(i, ["pokemon"]) for i in range(14, 20)]
    m = T.plan(vault, old + new, m, fake_ask(assign=lambda ns: {str(n): "pokemon Hub" for n in ns}))
    old_hub = m["hubs"]["pokemon Hub"]
    assert old_hub["superseded_by"] and not old_hub["members"]
    assert all(len(m["hubs"][n]["members"]) <= T.MAX_NOTES for n in old_hub["superseded_by"])


def test_new_notes_claude_calls_new_wait_until_five_then_form_a_hub(vault):
    old = [note(i, ["pokemon"]) for i in range(6)]
    m = T.plan(vault, old, {}, fake_ask())
    four = [note(i, ["pokemon"]) for i in range(6, 10)]
    m = T.plan(vault, old + four, m, fake_ask())  # all NEW, only 4
    assert len(m["hubs"]) == 1
    five = four + [note(10, ["pokemon"])]
    m = T.plan(vault, old + five, m, fake_ask())
    assert len(m["hubs"]) == 2


# --- the vault ---

def test_old_tag_hub_is_adopted_but_a_media_hub_is_left_alone(vault):
    hubs = vault / "Hubs"
    (hubs / "pokemon Hub.md").write_text(
        f"{START}\n*Synthesised from 2 notes tagged pokemon. Edits…*\n\ntext\n\n"
        f"## Notes in this hub\n- [[Note 1]]\n- [[Note 2]]\n{END}\n", encoding="utf-8")
    (hubs / "Some Show Hub.md").write_text(
        f"{START}\n*Synthesised from 8 notes tagged Some Show. Edits…*\n{END}\n", encoding="utf-8")
    notes = [note(i, ["pokemon"]) for i in range(1, 7)]
    m = T.adopt_legacy(vault, notes, {})
    assert list(m["hubs"]) == ["pokemon Hub"]
    assert m["hubs"]["pokemon Hub"]["members"] == ["vid00000001", "vid00000002"]


def test_name_clash_with_a_page_we_do_not_own_gets_a_suffix(vault):
    (vault / "Hubs" / "some-show Hub.md").write_text("hand-written", encoding="utf-8")
    m = T.plan(vault, [note(i, ["some-show"]) for i in range(6)], {}, fake_ask())
    assert list(m["hubs"]) == ["some-show (topic) Hub"]


def test_superseded_hub_page_points_to_its_replacements_and_keeps_own_text(vault):
    page = vault / "Hubs" / "pokemon Hub.md"
    page.write_text(f"My own words.\n\n{START}\nold synthesis\n{END}\n", encoding="utf-8")
    T.write_superseded(vault, "pokemon Hub", ["pokemon - Theme 1 Hub", "pokemon - Theme 2 Hub"])
    text = page.read_text(encoding="utf-8")
    assert text.startswith("My own words.")
    assert "[[pokemon - Theme 1 Hub]]" in text and "old synthesis" not in text


def test_old_hub_over_the_cap_releases_its_notes_to_their_own_topics(vault):
    listed = "\n".join(f"- [[Note {i}]]" for i in range(20))
    (vault / "Hubs" / "video-game Hub.md").write_text(
        f"{START}\n*Synthesised from 20 notes tagged video-game. Edits…*\n\n"
        f"## Notes in this hub\n{listed}\n{END}\n", encoding="utf-8")
    notes = ([note(i, ["video-game", "pokemon"]) for i in range(10)]
             + [note(i, ["video-game", "zelda"]) for i in range(10, 20)])
    m = T.plan(vault, notes, T.adopt_legacy(vault, notes, {}), fake_ask())
    assert m["hubs"]["video-game Hub"]["superseded_by"] == ["pokemon Hub", "zelda Hub"]
    assert T.needs_superseded_page(m) == ["video-game Hub"]
    assert "video-game Hub" not in T.needs_writing(m)


def test_old_hub_linking_a_note_twice_adopts_it_once(vault):
    (vault / "Hubs" / "pokemon Hub.md").write_text(
        f"{START}\n*Synthesised from 2 notes tagged pokemon. Edits…*\n\n"
        f"- [[Note 1]] argues X\n\n## Notes in this hub\n- [[Note 1]]\n- [[Note 2]]\n{END}\n",
        encoding="utf-8")
    m = T.adopt_legacy(vault, [note(i, ["pokemon"]) for i in range(1, 7)], {})
    assert m["hubs"]["pokemon Hub"]["members"] == ["vid00000001", "vid00000002"]
