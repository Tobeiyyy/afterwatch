from pathlib import Path

import pytest

from afterwatch import resurface as R
from afterwatch.hubs import END, START, build_prompt, hub_tag, render, upsert


def note(title, tags, body="body text"):
    return R.Note(path=Path(f"{title}.md"), title=title, tags=list(tags), body=body)


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "Hubs").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cluster():
    return R.Cluster(
        tags=["comp-gaming", "rocket-league", "gaming"],
        notes=[note("Alpha", ["comp-gaming", "rocket-league"]),
               note("Beta", ["comp-gaming", "rocket-league", "gaming"])],
    )


def test_hub_tag_prefers_cluster_coverage_over_vault_rarity(cluster):
    """"gaming" is on fewer notes vault-wide but only half the cluster, so the
    tag the cluster agrees on wins."""
    corpus = cluster.notes + [note("Unrelated", ["gaming"])]
    assert hub_tag(cluster, corpus) == "comp-gaming"


def test_hub_tag_demotes_a_tag_borrowed_from_elsewhere():
    c = R.Cluster(tags=["niche", "self-improvement"],
                  notes=[note("A", ["niche", "self-improvement"]),
                         note("B", ["niche", "self-improvement"])])
    corpus = c.notes + [note(f"Other{i}", ["self-improvement"]) for i in range(9)]
    assert hub_tag(c, corpus) == "niche"


def test_regeneration_replaces_only_the_marked_block(vault, cluster):
    path = upsert(vault, cluster, "First synthesis.", cluster.notes)
    path.write_text(
        "# My own heading\n\nKeep me above.\n\n"
        + path.read_text(encoding="utf-8")
        + "\n\nKeep me below too.\n",
        encoding="utf-8",
    )

    upsert(vault, cluster, "Second synthesis.", cluster.notes)
    after = path.read_text(encoding="utf-8")

    assert "Keep me above." in after
    assert "Keep me below too." in after
    assert "Second synthesis." in after
    assert "First synthesis." not in after
    assert after.count(START) == 1 and after.count(END) == 1


def test_hand_written_page_without_markers_is_appended_to_not_clobbered(vault, cluster):
    from afterwatch.hubs import hub_path
    path = hub_path(vault, cluster, cluster.notes)
    path.write_text("Notes I wrote myself.\n", encoding="utf-8")

    upsert(vault, cluster, "Generated.", cluster.notes)
    after = path.read_text(encoding="utf-8")

    assert after.startswith("Notes I wrote myself.")
    assert "Generated." in after


def test_render_links_every_note(cluster):
    out = render(cluster, "synthesis")
    for n in cluster.notes:
        assert f"[[{n.title}]]" in out


def test_prompt_carries_titles_and_forbids_per_note_sections(cluster):
    prompt = build_prompt(cluster)
    assert "[[Alpha]]" in prompt and "[[Beta]]" in prompt
    assert "bullet-per-video" in prompt
    assert "at least two notes" in prompt


def test_prompt_truncates_a_long_note_without_dropping_it():
    big = R.Cluster(tags=["t"], notes=[note("Huge", ["t"], "word " * 4000)])
    prompt = build_prompt(big)
    assert "[[Huge]]" in prompt
    assert "[...truncated]" in prompt
    assert len(prompt) < 6000
