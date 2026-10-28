"""One checklist note listing every video note, grouped by topic and hub.

    afterwatch reading-order [--vault PATH]

Writes "Reading Order.md" in the vault root. Topics and hubs come from the
topic-hub manifest; topics whose videos were watched most come first, each hub
before its notes. Notes without a hub follow at the end. Ticked boxes survive a
rebuild, and anything written outside the generated block is left alone.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import config, topic_hubs
from . import resurface as R
from .backfill import BackfillError, load_state, resolve_vault
from .hubs import upsert_block

PAGE_NAME = "Reading Order.md"
START = "<!-- afterwatch:reading-order:start -->"
END = "<!-- afterwatch:reading-order:end -->"
_TICKED = re.compile(r"^- \[[xX]\] \[\[([^\]]+)\]\]", re.MULTILINE)


def build(vault: Path) -> str:
    """The generated block, markers included."""
    notes = R.load_notes(vault)
    by_vid = {n.video_id: n for n in notes if n.video_id}
    hubs = {k: h for k, h in topic_hubs.load_manifest().get("hubs", {}).items()
            if not h.get("superseded_by") and not h.get("released") and h.get("members")}
    watched = {v: h["watch_count"] for v, h in load_state("history.json", {}).items()}
    page = vault / PAGE_NAME
    ticked = set(_TICKED.findall(page.read_text(encoding="utf-8", errors="replace"))) \
        if page.exists() else set()

    def score(vids) -> int:
        return sum(watched.get(v, 1) for v in vids if v in by_vid)

    topics: dict[str, list[str]] = {}
    for name, hub in hubs.items():
        topics.setdefault(hub["topic"], []).append(name)

    seen: set[str] = set()   # a note in two hubs is listed once, under the first
    lines: list[str] = []

    def item(n: R.Note) -> None:
        lines.append(f"- [{'x' if n.title in ticked else ' '}] [[{n.title}]]")

    for topic in sorted(topics, key=lambda t: (-score(v for h in topics[t] for v in hubs[h]["members"]),
                                               t.lower())):
        sections = []
        for name in sorted(topics[topic], key=lambda h: (-score(hubs[h]["members"]), h.lower())):
            members = sorted((by_vid[v] for v in dict.fromkeys(hubs[name]["members"])
                              if v in by_vid and v not in seen),
                             key=lambda n: (-watched.get(n.video_id, 1), n.title.lower()))
            seen.update(n.video_id for n in members)
            if members:
                sections.append((name, members))
        if sections:
            lines.append(f"\n## {topic} ({sum(len(m) for _, m in sections)} notes)")
            for name, members in sections:
                lines.append(f"\n**[[{name}]]**")
                for n in members:
                    item(n)

    loose = sorted((n for n in notes if n.video_id not in seen),
                   key=lambda n: (-watched.get(n.video_id, 1), n.title.lower()))
    if loose:
        lines.append(f"\n## Without a topic ({len(loose)} notes)")
        for n in loose:
            item(n)

    head = (f"*{len(notes)} video notes, grouped by topic and hub; read each hub before its "
            "notes. Tick what you have read: ticks survive a rebuild.*")
    return f"{START}\n{head}\n" + "\n".join(lines) + f"\n{END}\n"


def write(vault: Path) -> Path:
    return upsert_block(vault / PAGE_NAME, build(vault), START, END)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="afterwatch reading-order", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="vault or notes folder (default: output.path)")
    args = ap.parse_args(argv)
    try:
        vault = resolve_vault(args.out, config.load())
    except BackfillError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    path = write(vault)
    text = path.read_text(encoding="utf-8")
    print(f"wrote {path}: {text.count(chr(10) + '- [')} notes, "
          f"{len(_TICKED.findall(text))} ticked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
