"""Reading video notes back from the notes folder: tags, video id, body.

Hubs, the reading order and the backfill use this to see what already exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from . import config


# Tags describing the note's form rather than its subject. They appear on nearly
# every note, so grouping on them would put everything with everything.
STRUCTURAL_TAGS = {
    "youtube", "mini-essay", "overview", "list", "info",
    "guide", "review", "analysis", "deep-dive", "opinion", "thoughts",
    "media",
}

_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")
_OLD_TAGS_LINE = re.compile(r"^\s*Tags:\s*(.+)$", re.MULTILINE)
_NEW_TAGS_LINE = re.compile(r"^\s*\*\*Tags:\*\*\s*(.+)$", re.MULTILINE)
_AUTHOR_LINE = re.compile(r"^\s*\*\*Author:\*\*\s*(.+)$", re.MULTILINE)
_IFRAME_ID = re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{11})")
_WATCH_ID = re.compile(r"(?:youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})")
_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass
class Note:
    """One parsed video note."""

    path: Path
    title: str
    tags: list[str] = field(default_factory=list)
    video_id: str | None = None
    author: str | None = None
    body: str = ""

    @property
    def topical_tags(self) -> set[str]:
        return {t for t in self.tags if t.lower() not in STRUCTURAL_TAGS}

    @property
    def url(self) -> str | None:
        return f"https://youtu.be/{self.video_id}" if self.video_id else None

    def obsidian_uri(self, vault_name: str) -> str:
        return f"obsidian://open?vault={quote(vault_name)}&file={quote(self.path.stem)}"


def _extract_tags(text: str) -> list[str]:
    """Tags come from a 'Tags:' or '**Tags:**' line as wikilinks. Order is kept,
    duplicates dropped — some older notes repeat a tag."""
    match = _NEW_TAGS_LINE.search(text) or _OLD_TAGS_LINE.search(text)
    if not match:
        return []
    seen: set[str] = set()
    tags: list[str] = []
    for raw in _WIKILINK.findall(match.group(1)):
        tag = raw.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def _extract_video_id(text: str) -> str | None:
    for pattern in (_IFRAME_ID, _WATCH_ID):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def _extract_body(text: str) -> str:
    """Everything after the metadata header and before the References section."""
    body = text
    cut = body.find("\n# References")
    if cut != -1:
        body = body[:cut]

    lines = body.splitlines()
    start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip metadata: bare timestamps, Tags/Source/Author/Schema lines, and
        # the H1 title (the filename already carries it in Obsidian).
        if (
            re.fullmatch(r"\d{4}-\d{2}-\d{2}(\s+\d{2}:\d{2})?", stripped)
            or stripped.startswith(("Tags:", "**Tags:**", "**Source:**",
                                    "**Author:**", "**Schema Applied:**"))
            or stripped.startswith("# ")
        ):
            start = i + 1
            continue
        break
    return "\n".join(lines[start:]).strip()


def parse_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8", errors="replace")
    author = _AUTHOR_LINE.search(text)
    return Note(
        path=path,
        title=path.stem,
        tags=_extract_tags(text),
        video_id=_extract_video_id(text),
        author=author.group(1).strip() if author else None,
        body=_extract_body(text),
    )


def load_notes(vault: Path) -> list[Note]:
    videos_dir = vault / config.folder("notes")
    if not videos_dir.is_dir():
        return []
    notes: list[Note] = []
    for path in sorted(videos_dir.glob("*.md")):
        try:
            notes.append(parse_note(path))
        except OSError:
            continue
    return notes


@dataclass
class Cluster:
    """A group of notes one hub is written from."""
    tags: list[str]
    notes: list[Note]

    @property
    def size(self) -> int:
        return len(self.notes)

    @property
    def label(self) -> str:
        return " + ".join(self.tags[:3])
