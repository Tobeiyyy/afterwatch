from __future__ import annotations

import pytest

from afterwatch import backfill, reading_order as ro, topic_hubs


def vid(n: int) -> str:
    return f"vid{n:08d}"


def note(vault, n: int, title: str | None = None) -> str:
    title = title or f"Note {n}"
    (vault / "Videos").mkdir(exist_ok=True)
    (vault / "Videos" / f"{title}.md").write_text(
        f'**Tags:** [[x]]\n\nbody\n\n# References\n<iframe src="https://www.youtube.com/embed/{vid(n)}">'
        "</iframe>\n", encoding="utf-8")
    return title


def manifest(**hubs):
    """hubs: name -> (topic, [video numbers], extra keys)"""
    topic_hubs.save_manifest({"hubs": {
        name: {"topic": topic, "members": [vid(i) for i in members], "written": [], **extra}
        for name, (topic, members, extra) in hubs.items()}})


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


def items(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- [")]


def test_groups_by_topic_and_hub_with_loose_notes_last(vault):
    for n in range(1, 7):
        note(vault, n)
    manifest(**{"Games - A Hub": ("Games", [1, 2], {}), "Games - B Hub": ("Games", [3], {}),
                "Money Hub": ("Money", [4, 5], {})})
    text = ro.build(vault)
    assert text.startswith(ro.START) and text.rstrip().endswith(ro.END)
    order = [text.index(s) for s in ("## Games (3 notes)", "**[[Games - A Hub]]**",
                                     "**[[Games - B Hub]]**", "## Money (2 notes)",
                                     "**[[Money Hub]]**", "## Without a topic (1 notes)")]
    assert order == sorted(order)
    assert items(text) == ["- [ ] [[Note 1]]", "- [ ] [[Note 2]]", "- [ ] [[Note 3]]",
                           "- [ ] [[Note 4]]", "- [ ] [[Note 5]]", "- [ ] [[Note 6]]"]
    assert text.index("[[Note 6]]") > text.index("Without a topic")


def test_each_note_is_listed_once_even_in_two_hubs(vault):
    for n in range(1, 4):
        note(vault, n)
    manifest(**{"A Hub": ("Games", [1, 2], {}), "B Hub": ("Games", [2, 3], {})})
    text = ro.build(vault)
    assert all(text.count(f"[[Note {n}]]") == 1 for n in (1, 2, 3))
    assert text.index("[[Note 2]]") < text.index("**[[B Hub]]**")      # under the first hub


def test_dead_hubs_missing_notes_and_empty_hubs_are_ignored(vault):
    note(vault, 1)
    note(vault, 2)
    manifest(**{"Old Hub": ("Games", [1], {"superseded_by": ["New Hub"]}),
                "Gone Hub": ("Games", [1], {"released": [vid(1)]}),
                "Ghost Hub": ("Games", [90, 91], {}),
                "Live Hub": ("Games", [2, 92], {})})
    text = ro.build(vault)
    assert "Old Hub" not in text and "Gone Hub" not in text and "Ghost Hub" not in text
    assert items(text) == ["- [ ] [[Note 2]]", "- [ ] [[Note 1]]"]
    assert "## Games (1 notes)" in text and "Without a topic (1 notes)" in text


def test_no_manifest_lists_everything_without_a_topic(vault):
    note(vault, 1)
    note(vault, 2)
    text = ro.build(vault)
    assert "Without a topic (2 notes)" in text and len(items(text)) == 2


def test_empty_vault_still_builds(vault):
    text = ro.build(vault)
    assert items(text) == [] and "0 video notes" in text


def test_notes_without_a_video_id_still_appear(vault):
    note(vault, 1)
    (vault / "Videos" / "Hand written.md").write_text("no link here", encoding="utf-8")
    assert "[[Hand written]]" in ro.build(vault)


def test_most_watched_first(vault):
    for n in range(1, 5):
        note(vault, n)
    manifest(**{"A Hub": ("Quiet", [1, 2], {}), "B Hub": ("Loud", [3, 4], {})})
    backfill.save_state("history.json", {vid(4): {"watch_count": 6}, vid(3): {"watch_count": 2}})
    text = ro.build(vault)
    assert items(text) == ["- [ ] [[Note 4]]", "- [ ] [[Note 3]]",
                           "- [ ] [[Note 1]]", "- [ ] [[Note 2]]"]
    assert text.index("## Loud") < text.index("## Quiet")


def test_ticks_survive_a_rewrite_and_outside_text_is_kept(vault):
    for n in range(1, 3):
        note(vault, n)
    manifest(**{"A Hub": ("Games", [1, 2], {})})
    page = ro.write(vault)
    assert page == vault / "Reading Order.md"
    text = page.read_text(encoding="utf-8")
    page.write_text("my intro\n\n" + text.replace("- [ ] [[Note 2]]", "- [x] [[Note 2]]")
                    + "\nmy outro\n", encoding="utf-8")
    note(vault, 3)                                    # a new note arrives
    manifest(**{"A Hub": ("Games", [1, 2, 3], {})})
    ro.write(vault)
    again = page.read_text(encoding="utf-8")
    assert "- [x] [[Note 2]]" in again and "- [ ] [[Note 1]]" in again and "- [ ] [[Note 3]]" in again
    assert again.startswith("my intro") and again.rstrip().endswith("my outro")
    assert again.count(ro.START) == 1


def test_main_writes_the_page(vault, capsys):
    note(vault, 1)
    assert ro.main(["--out", str(vault)]) == 0
    assert (vault / "Reading Order.md").exists()
    assert "1 notes" in capsys.readouterr().out


def test_main_needs_a_vault(monkeypatch, capsys):
    assert ro.main([]) == 2
    assert "no output folder" in capsys.readouterr().err
