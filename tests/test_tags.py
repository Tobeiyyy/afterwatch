from afterwatch.tags import load_tags


def test_load_tags_reads_md_stems_sorted(tmp_path):
    tags_dir = tmp_path / "Tags"
    tags_dir.mkdir()
    for name in ("zebra.md", "anime.md", "self-improvement.md"):
        (tags_dir / name).write_text("", encoding="utf-8")
    (tags_dir / "not-a-tag.txt").write_text("", encoding="utf-8")
    assert load_tags(tmp_path) == ["anime", "self-improvement", "zebra"]


def test_load_tags_missing_folder_returns_empty(tmp_path):
    assert load_tags(tmp_path) == []
