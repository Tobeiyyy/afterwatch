"""All vault filesystem writes: notes, tag stubs, and the failure note."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from . import config


# Windows-invalid chars plus Obsidian-reserved ones (# ^ [ ] |)
_INVALID = re.compile(r'[<>:"/\\|?*#^\[\]\x00-\x1f]')


def sanitize_filename(title: str) -> str:
    name = _INVALID.sub("", title)
    name = name.strip(" .")
    return name or "Untitled"


def find_existing_note(vault: Path, video_id: str) -> Path | None:
    videos_dir = vault / config.folder("notes")
    if not videos_dir.is_dir():
        return None
    for path in videos_dir.glob("*.md"):
        try:
            if video_id in path.read_text(encoding="utf-8", errors="replace"):
                return path
        except OSError:
            continue
    return None


def _references_section(video_id: str) -> str:
    return (
        "\n\n# References\n"
        f'<iframe width="560" height="315" '
        f'src="https://www.youtube.com/embed/{video_id}" '
        'title="YouTube video player" frameborder="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; '
        'gyroscope; picture-in-picture; web-share" '
        'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen>'
        "</iframe>\n"
    )


def _strip_title_heading(note: str) -> str:
    """The filename is the note's title in Obsidian; drop a leading H1 so the
    title doesn't appear twice (defense in case the model emits one anyway)."""
    text = note.lstrip()
    if text.startswith("# "):
        first_newline = text.find("\n")
        if first_newline == -1:
            return ""
        return text[first_newline + 1 :].lstrip("\n")
    return note


def write_note(vault: Path, title: str, video_id: str, note: str) -> Path:
    videos_dir = vault / config.folder("notes")
    videos_dir.mkdir(parents=True, exist_ok=True)
    base = sanitize_filename(title)
    path = videos_dir / f"{base}.md"
    if path.exists():
        # Same title, different video (same video is deduped upstream by ID).
        path = videos_dir / f"{base} ({video_id}).md"
    path.write_text(
        _strip_title_heading(note).rstrip() + _references_section(video_id),
        encoding="utf-8",
    )
    return path


def create_tag_stubs(vault: Path, tags: list[str]) -> list[str]:
    """Create an empty '<tags folder>/<tag>.md' per new tag (the vault's tag files are
    empty backlink collectors). Returns the tags actually created."""
    tags_dir = vault / config.folder("tags")
    tags_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    for tag in tags:
        stub = tags_dir / f"{sanitize_filename(tag)}.md"
        if not stub.exists():
            stub.write_text("", encoding="utf-8")
            created.append(tag)
    return created


def append_failure(vault: Path, url: str, reason: str) -> None:
    path = vault / config.folder("failures")
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"- [{dt.date.today().isoformat()}] {url} — {reason}\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + entry, encoding="utf-8")
    else:
        path.write_text(
            "# Afterwatch — Failed videos\n\n" + entry,
            encoding="utf-8",
        )
