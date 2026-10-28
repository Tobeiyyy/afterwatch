"""Write a synthesis hub page for a group of notes.

Topic hubs (``topic_hubs``) decide which notes belong together; this module
builds the prompt and writes the page. It also makes a hub for one film, series
or game from a list of its videos (``afterwatch hubs media``).

A hub is NOT a pile of summaries — those already exist one per note and reading
them in sequence adds nothing. It is organised by claim: what several notes
independently land on, where they disagree, and which single take is strongest.
Every claim carries inline ``[[wikilinks]]`` to the notes it came from, so the
hub stays navigable rather than becoming a lossy replacement for its sources.

Only the region between the markers is generated. Anything written by hand
outside it survives regeneration untouched.

    afterwatch hubs media "Some Show" --links videos.txt            # print, write nothing
    afterwatch hubs media "Some Show" --links videos.txt --write    # save the hub
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import resurface as R
from . import llm
from .claude_cli import ClaudeCliError
from . import config

START = "<!-- afterwatch:hub:start -->"
END = "<!-- afterwatch:hub:end -->"

# Enough of each note for the model to reason about its argument. Whole notes
# would blow the prompt up on a large cluster for little gain -- the claims live
# near the top.
NOTE_BUDGET = 2500


def hub_tag(cluster: R.Cluster, notes: list[R.Note] | None = None) -> str:
    """The tag that best names the page.

    Rank by how much of the cluster carries the tag, then by how much of that
    tag's vault-wide use is inside this cluster. Coverage alone picks tags that
    are merely broad; concentration alone picks whichever happens to be rare
    ("gaming", on 5 notes, beat "rocket-league" on 6). Together they demote both
    the generic tag and the one borrowed from elsewhere. Genuine ties -- like
    "comp-gaming" and "rocket-league", identical on both measures -- fall back to
    alphabetical, and either name is fine.
    """
    if not notes:
        return cluster.tags[0]
    corpus: dict[str, int] = {}
    for n in notes:
        for t in n.topical_tags:
            corpus[t.lower()] = corpus.get(t.lower(), 0) + 1
    inside: dict[str, int] = {}
    for n in cluster.notes:
        for t in n.topical_tags:
            inside[t.lower()] = inside.get(t.lower(), 0) + 1

    def rank(tag: str):
        key = tag.lower()
        here = inside.get(key, 0)
        return (-here, -(here / corpus.get(key, 1)), tag)

    return min(cluster.tags, key=rank)


def hub_title(cluster: R.Cluster, notes: list[R.Note] | None = None) -> str:
    return f"{hub_tag(cluster, notes)} Hub"


def hub_path(vault: Path, cluster: R.Cluster,
             notes: list[R.Note] | None = None) -> Path:
    from .writer import sanitize_filename
    return vault / config.folder("hubs") / f"{sanitize_filename(hub_title(cluster, notes))}.md"


def build_prompt(cluster: R.Cluster) -> str:
    parts = [
        "You are writing a synthesis hub page for an Obsidian vault. It sits above "
        f"{len(cluster.notes)} notes, each summarising one YouTube video on a shared subject.\n",
        "The per-video notes already exist and the reader can open any of them. A hub that "
        "restates them is worthless. Organise by CLAIM instead:\n",
        "  - What do several notes independently converge on? State the claim in your own "
        "words, then cite the notes backing it.",
        "  - Where do they contradict each other, or emphasise incompatible things? Name the "
        "disagreement plainly rather than smoothing it over.",
        "  - Which single note makes the strongest or most original argument, and why that one?",
        "  - What does the group as a whole leave unanswered?\n",
        "Rules:",
        "  - Cite notes inline as [[Exact Note Title]], using the titles exactly as given below.",
        "  - Never write a per-note section, a bullet-per-video list, or anything that reads "
        "as summaries in sequence.",
        "  - A claim only counts as convergence if at least two notes support it. Say so when "
        "only one does.",
        "  - No preamble, no closing summary, no restating this instruction. Start with the "
        "first claim.",
        "  - Markdown with `##` section headings. Do not emit an H1 — the filename is the title.",
        "  - If the notes genuinely share too little to synthesise, say that in one line "
        "instead of manufacturing themes.\n",
        "=" * 60,
        f"SHARED TAGS: {', '.join(cluster.tags)}",
        "=" * 60,
    ]
    for note in cluster.notes:
        body = note.body.strip()
        if len(body) > NOTE_BUDGET:
            body = body[:NOTE_BUDGET].rsplit("\n", 1)[0] + "\n[...truncated]"
        parts.append(f"\n### [[{note.title}]]")
        if note.author:
            parts.append(f"Channel: {note.author}")
        parts.append(f"Tags: {', '.join(sorted(note.topical_tags)) or '(none)'}")
        parts.append(body or "(no body text)")
    return "\n".join(parts)


def render(cluster: R.Cluster, generated: str) -> str:
    """The generated block, with its source list and markers."""
    links = "\n".join(f"- [[{n.title}]]" for n in cluster.notes)
    return (
        f"{START}\n"
        f"*Synthesised from {len(cluster.notes)} notes tagged "
        f"{', '.join(cluster.tags)}. Edits inside this block are overwritten on "
        f"regeneration; write anything you want to keep outside it.*\n\n"
        f"{generated.strip()}\n\n"
        f"## Notes in this hub\n{links}\n"
        f"{END}\n"
    )


def upsert_block(path: Path, block: str, start: str = START, end: str = END) -> Path:
    """Write ``block`` to ``path``, replacing only the region between the markers.

    Shared by every generator that owns a marked region in a vault page (hubs,
    trope digests). ``block`` must itself begin with ``start`` and end with
    ``end``. Anything outside the markers is never touched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = path.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.DOTALL)
        if pattern.search(existing):
            path.write_text(pattern.sub(lambda _m: block, existing, count=1),
                            encoding="utf-8")
            return path
        # Hand-written page with no marked region yet: append, never clobber.
        path.write_text(existing.rstrip() + "\n\n" + block, encoding="utf-8")
        return path

    path.write_text(block, encoding="utf-8")
    return path


def upsert(vault: Path, cluster: R.Cluster, generated: str,
           notes: list[R.Note] | None = None) -> Path:
    """Write the hub, replacing only the generated region of an existing page."""
    return upsert_block(hub_path(vault, cluster, notes), render(cluster, generated))


def media_cluster(vault: Path, media: str, links_file: Path) -> R.Cluster:
    """A hub for one film/series/game rather than a tag cluster.

    Tags can't find these: notes about one show are tagged by what they
    discuss, rarely by the show's name, so the video ids come in from a list.
    """
    wanted = set(re.findall(r"[A-Za-z0-9_-]{11}", links_file.read_text(encoding="utf-8")))
    notes = [n for n in R.load_notes(vault) if n.video_id in wanted]
    missing = wanted - {n.video_id for n in notes}
    if missing:
        print(f"note missing for {len(missing)} video(s): {', '.join(sorted(missing))}",
              file=sys.stderr)
    if not notes:
        raise SystemExit("none of those videos have a note in the vault yet")
    return R.Cluster(tags=[media], notes=notes)


def main(argv: list[str] | None = None) -> int:
    """`afterwatch hubs media NAME --links FILE [--prompt | --from-file PATH] [--write]`."""
    from .cli import resolve_output
    ap = argparse.ArgumentParser(prog="afterwatch hubs media", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media", help="the film, series or game the hub is about")
    ap.add_argument("--links", required=True,
                    help="file of video URLs/ids belonging to that media")
    ap.add_argument("--prompt", action="store_true",
                    help="print the prompt and exit without calling the model")
    ap.add_argument("--write", action="store_true", help="save the hub")
    ap.add_argument("--from-file", metavar="PATH",
                    help="use already-generated text instead of calling the model, "
                         "so what you reviewed is what gets written")
    ap.add_argument("--model", help="override the configured hubs model for this run")
    ap.add_argument("--out", help="notes folder or vault (default: output.path)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        vault = resolve_output(args.out)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    cluster = media_cluster(vault, args.media, Path(args.links))
    prompt = build_prompt(cluster)
    if args.prompt:
        print(prompt)
        return 0

    if args.from_file:
        generated = Path(args.from_file).read_text(encoding="utf-8")
    else:
        print(f"Generating hub for {cluster.size} notes ({cluster.label})...",
              file=sys.stderr, flush=True)
        try:
            generated = llm.complete("hubs", prompt, model=args.model)
        except ClaudeCliError as exc:
            print(f"generation failed: {exc}", file=sys.stderr)
            return 1

    if args.write:
        print(f"wrote {upsert(vault, cluster, generated)}")
    else:
        print(render(cluster, generated))
        print("\n(nothing written — pass --write to save)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
