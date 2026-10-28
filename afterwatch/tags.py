"""Live tag list from the vault's tags folder (filename = tag)."""
from __future__ import annotations

from pathlib import Path
from . import config



def load_tags(vault: Path) -> list[str]:
    tags_dir = vault / config.folder("tags")
    if not tags_dir.is_dir():
        return []
    return sorted(p.stem for p in tags_dir.glob("*.md"))
