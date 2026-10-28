"""Topic hubs: one capped synthesis page per theme, kept current as notes arrive.

The old clustering chained notes that shared two tags; at ~2,000 notes that
turned almost the whole vault into one cluster. Here every note gets one topic
(its most specific tag that at least MIN_NOTES notes share). A topic of up to
MAX_NOTES notes is one hub. Bigger topics are split by the model into themes from
the note titles, so a hub's notes really belong together.

Membership lives in a manifest. New notes are placed into an existing hub of
their topic (the model picks which) or wait until MIN_NOTES of them can start a new
one. A hub is rewritten once REWRITE_AFTER notes have joined; a hub that grows
past the cap is split and its page points to the replacements. Pages we don't
own (media hubs, hand-written pages) are never touched, and text outside the
generated markers survives every rewrite.

The limits come from the [hubs] settings. Commands:

    afterwatch hubs                  # plan and write what changed
    afterwatch hubs plan             # decide membership only (few calls)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import hubs as H
from . import resurface as R
from . import config, llm
from .claude_cli import ClaudeCliError
from .writer import sanitize_filename

MIN_NOTES = 5
MAX_NOTES = 15
REWRITE_AFTER = 3
# Form, not subject: on hundreds of notes each, they'd make one giant "topic".
FORMAT_TAGS = {"video-essay", "ranking", "retrospective"}
def manifest_path() -> Path:
    return config.state_dir() / "topic-hubs.json"

_HEADER = re.compile(r"Synthesised from \d+ notes tagged ([^.\n]+)\.")
_LISTED = re.compile(r"^- \[\[([^\]]+)\]\]", re.MULTILINE)


# --- topics ---

def _tags(note: R.Note) -> set[str]:
    return {t.lower() for t in note.topical_tags} - FORMAT_TAGS


def tag_counts(notes: list[R.Note]) -> Counter:
    return Counter(t for n in notes for t in _tags(n))


def topic_of(note: R.Note, counts) -> str | None:
    """The most specific tag shared by at least MIN_NOTES notes."""
    shared = [t for t in _tags(note) if counts.get(t, 0) >= MIN_NOTES]
    return min(shared, key=lambda t: (counts[t], t)) if shared else None


# --- asking the model ---

def ask_model(prompt: str) -> dict:
    text = llm.complete("hub_planning", prompt)
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1]) if start != -1 else {}
    except ValueError:
        return {}


def _numbered(notes: list[R.Note]) -> str:
    return "\n".join(f"{i}. {n.title}" for i, n in enumerate(notes, 1))


def _even_chunks(notes: list, size: int) -> list[list]:
    parts = math.ceil(len(notes) / size)
    step = math.ceil(len(notes) / parts)
    return [notes[i:i + step] for i in range(0, len(notes), step)]


def split(topic: str, notes: list[R.Note], ask, depth: int = 0) -> list[tuple[str, list[R.Note]]]:
    """Themes of MIN_NOTES..MAX_NOTES notes. Whatever the model leaves out, repeats
    or over-fills is repaired here, so every note lands in exactly one theme."""
    reply = ask(
        f'These {len(notes)} notes summarise YouTube videos that share the topic "{topic}". '
        f"Group them into themes of {MIN_NOTES}-{MAX_NOTES} notes each, by what the videos "
        "are actually about, so each theme can get its own synthesis page. Every number "
        "goes into exactly one theme. Theme names: 2-5 plain words, without repeating the "
        "topic.\n"
        'Answer with ONLY JSON: {"themes": [{"name": "...", "notes": [1, 2, 3]}]}\n\n'
        + _numbered(notes))
    seen, themes = set(), []
    for theme in reply.get("themes", []) if isinstance(reply, dict) else []:
        idx = []
        for i in theme.get("notes", []) if isinstance(theme, dict) else []:
            if isinstance(i, int) and 1 <= i <= len(notes) and i not in seen:
                seen.add(i)
                idx.append(i)
        if idx:
            themes.append((str(theme.get("name") or "Other").strip()[:60], [notes[i - 1] for i in idx]))

    leftover = [n for i, n in enumerate(notes, 1) if i not in seen]
    out = []
    for name, group in themes:
        if len(group) > MAX_NOTES:
            out += (split(topic, group, ask, depth + 1) if depth < 1 else
                    [(f"{name} ({k})", c) for k, c in enumerate(_even_chunks(group, MAX_NOTES), 1)])
        elif len(group) < MIN_NOTES:
            leftover += group
        else:
            out.append((name, group))

    if len(leftover) >= MIN_NOTES or not out:
        chunks = _even_chunks(leftover, MAX_NOTES) if leftover else []
        out += [("Other" if len(chunks) == 1 else f"Other ({k})", c) for k, c in enumerate(chunks, 1)]
    else:
        for n in leftover:  # too few for their own hub: top up the smallest themes
            group = min((g for _, g in out), key=len)
            group.append(n)
    return out


# --- planning ---

def _active(m: dict) -> dict:
    return {k: h for k, h in m["hubs"].items()
            if not h.get("superseded_by") and not h.get("released")}


def _unique_name(vault: Path, m: dict, stem: str) -> str:
    """`stem Hub`, unless that page exists and isn't ours or the name is taken."""
    for suffix in ["", " (topic)"] + [f" ({k})" for k in range(2, 50)]:
        name = f"{stem}{suffix} Hub"
        page = vault / config.folder("hubs") / f"{sanitize_filename(name)}.md"
        if name not in m["hubs"] and not page.exists():
            return name
    raise RuntimeError(f"no free hub name for {stem}")


def _new_hub(vault: Path, m: dict, stem: str, topic: str, notes: list[R.Note]) -> str:
    name = _unique_name(vault, m, stem)
    m["hubs"][name] = {"topic": topic, "members": [n.video_id for n in notes], "written": []}
    return name


def _found(vault, m, topic, notes, ask, themed: bool) -> list[str]:
    if not themed and len(notes) <= MAX_NOTES:
        return [_new_hub(vault, m, topic, topic, notes)]
    return [_new_hub(vault, m, f"{topic} - {theme}", topic, group)
            for theme, group in split(topic, notes, ask)]


def plan(vault: Path, notes: list[R.Note], manifest: dict, ask) -> dict:
    m = json.loads(json.dumps(manifest)) if manifest else {"hubs": {}}
    m.setdefault("hubs", {})
    counts = tag_counts(notes)
    by_vid = {n.video_id: n for n in notes if n.video_id}
    placed = {v for h in _active(m).values() for v in h["members"]}

    pending = defaultdict(list)
    for n in sorted(notes, key=lambda n: n.title):
        topic = topic_of(n, counts)
        if n.video_id and n.video_id not in placed and topic:
            pending[topic].append(n)

    for topic, new in pending.items():
        homes = {k: h for k, h in _active(m).items() if h["topic"] == topic}
        leftover = new
        if homes:
            sample = "\n".join(f"- {k}: " + "; ".join(by_vid[v].title for v in h["members"][:3] if v in by_vid)
                               for k, h in homes.items())
            reply = ask(
                f'New notes about "{topic}" need a home. Existing hubs (with a few of their notes):\n'
                f"{sample}\n"
                'For each new note, answer the exact hub name it fits, or "NEW" if none fits well.\n'
                'Answer with ONLY JSON: {"assign": {"1": "hub name or NEW"}}\n\n' + _numbered(new))
            picks = reply.get("assign", {}) if isinstance(reply, dict) else {}
            leftover = []
            for i, n in enumerate(new, 1):
                home = picks.get(str(i))
                if home in homes:
                    homes[home]["members"].append(n.video_id)
                else:
                    leftover.append(n)
        if len(leftover) >= MIN_NOTES:
            _found(vault, m, topic, leftover, ask, themed=bool(homes))

    for name, hub in list(m["hubs"].items()):
        if hub.get("released") and not hub.get("superseded_by"):
            homes = sorted({k for k, h in _active(m).items() if k != name
                            and set(h["members"]) & set(hub["released"])})
            if homes:
                hub["superseded_by"] = homes

    for name, hub in list(_active(m).items()):
        if len(hub["members"]) > MAX_NOTES:
            members = [by_vid[v] for v in hub["members"] if v in by_vid]
            hub["superseded_by"] = _found(vault, m, hub["topic"], members, ask, themed=True)
            hub["members"] = []
    return m


def mark_written(m: dict, name: str) -> None:
    m["hubs"][name]["written"] = list(m["hubs"][name]["members"])


def needs_writing(m: dict) -> list[str]:
    return [k for k, h in _active(m).items()
            if not h.get("written") or len(set(h["members"]) ^ set(h["written"])) >= REWRITE_AFTER]


def needs_superseded_page(m: dict) -> list[str]:
    return [k for k, h in m["hubs"].items() if h.get("superseded_by") and not h.get("superseded_written")]


# --- the vault ---

def adopt_legacy(vault: Path, notes: list[R.Note], m: dict) -> dict:
    """Take over hub pages the old tag clustering wrote, so they get rewritten or
    split instead of left stale. Media hubs ("tagged Some Show") are not tags and
    stay untouched."""
    m = m or {"hubs": {}}
    m.setdefault("hubs", {})
    by_title = {n.title: n.video_id for n in notes if n.video_id}
    for page in sorted((vault / config.folder("hubs")).glob("* Hub.md")):
        text = page.read_text(encoding="utf-8", errors="replace")
        header = _HEADER.search(text)
        stem = page.stem[:-len(" Hub")]
        if not header or page.stem in m["hubs"]:
            continue
        listed = [t.strip() for t in header.group(1).split(",")]
        if stem not in listed or any(t != t.lower() for t in listed):
            continue
        # The pattern also hits links inside the hub text, so a note can show twice.
        members = list(dict.fromkeys(by_title[t] for t in _LISTED.findall(text) if t in by_title))
        if len(members) > MAX_NOTES:
            # Too big to keep: its notes go to their own topics like any other,
            # and plan() points the page at wherever they ended up.
            m["hubs"][page.stem] = {"topic": stem, "members": [], "written": [],
                                    "released": members}
        else:
            m["hubs"][page.stem] = {"topic": stem, "members": members, "written": list(members)}
    return m


def hub_page(vault: Path, name: str) -> Path:
    return vault / config.folder("hubs") / f"{sanitize_filename(name)}.md"


def write_hub(vault: Path, m: dict, name: str, notes: list[R.Note]) -> tuple[Path, str, R.Cluster]:
    by_vid = {n.video_id: n for n in notes if n.video_id}
    hub = m["hubs"][name]
    cluster = R.Cluster(tags=[hub["topic"]], notes=[by_vid[v] for v in hub["members"] if v in by_vid])
    generated = llm.complete("hubs", H.build_prompt(cluster))
    path = H.upsert_block(hub_page(vault, name), H.render(cluster, generated))
    return path, generated, cluster


def write_superseded(vault: Path, name: str, replacements: list[str]) -> Path:
    links = "\n".join(f"- [[{r}]]" for r in replacements)
    block = (f"{H.START}\n*This hub grew past {MAX_NOTES} notes and was split up. "
             f"Its notes now live in:*\n\n{links}\n{H.END}\n")
    return H.upsert_block(hub_page(vault, name), block)


def load_manifest() -> dict:
    try:
        return json.loads(manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(m: dict) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
    tmp.replace(path)


def apply_settings(cfg: dict | None = None) -> None:
    """Take MIN_NOTES / MAX_NOTES / REWRITE_AFTER from the [hubs] settings."""
    global MIN_NOTES, MAX_NOTES, REWRITE_AFTER
    h = (cfg or config.load())["hubs"]
    MIN_NOTES, MAX_NOTES = int(h["min_notes"]), int(h["max_notes"])
    REWRITE_AFTER = int(h["rewrite_after"])


def _last_run_path() -> Path:
    return config.state_dir() / "hubs-last-run.txt"


def maybe_update(vault: Path, cfg: dict | None = None, log=print) -> list:
    """Run update() if hubs are on and ``update_every_days`` have passed since the
    last run, so normal runs keep hubs current without a scheduler."""
    cfg = cfg or config.load()
    if not cfg["hubs"]["enabled"]:
        return []
    try:
        last = dt.date.fromisoformat(_last_run_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        last = None
    today = dt.date.today()
    if last and (today - last).days < int(cfg["hubs"]["update_every_days"]):
        return []
    log("updating hubs (weekly)...")
    written = update(vault, int(cfg["hubs"]["max_per_run"]), 1, log, cfg)
    _last_run_path().parent.mkdir(parents=True, exist_ok=True)
    _last_run_path().write_text(today.isoformat(), encoding="utf-8")
    return written


def update(vault: Path, max_writes: int | None = None, workers: int = 1, log=print,
           cfg: dict | None = None):
    """Plan, then write what changed. Returns [(path, generated, cluster)] for
    every hub written, for whoever wants to announce them."""
    apply_settings(cfg)
    notes = R.load_notes(vault)
    m = load_manifest() or adopt_legacy(vault, notes, {})
    m = plan(vault, notes, m, ask_model)
    save_manifest(m)

    for name in needs_superseded_page(m):
        write_superseded(vault, name, m["hubs"][name]["superseded_by"])
        m["hubs"][name]["superseded_written"] = True
    save_manifest(m)

    todo = needs_writing(m)[:max_writes]
    lock, written = threading.Lock(), []

    def one(name):
        try:
            result = write_hub(vault, m, name, notes)
        except (ClaudeCliError, OSError) as exc:
            log(f"hub failed: {name}: {exc}")
            return
        with lock:
            mark_written(m, name)
            save_manifest(m)
            written.append(result)
            log(f"[{len(written)}/{len(todo)}] wrote {name}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, todo))
    return written


def main(argv: list[str] | None = None) -> int:
    """`afterwatch hubs [plan] [--max N] [--workers N] [--out PATH]`."""
    from .cli import resolve_output
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="afterwatch hubs", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=["plan", "write"], default="write")
    ap.add_argument("--max", type=int, help="write at most this many hubs "
                                            "(default: hubs.max_per_run)")
    ap.add_argument("--workers", type=int, default=1, help="hubs written in parallel")
    ap.add_argument("--out", help="notes folder or vault (default: output.path)")
    args = ap.parse_args(argv)
    cfg = config.load()
    try:
        vault = resolve_output(args.out, cfg)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    apply_settings(cfg)

    if args.action == "plan":
        notes = R.load_notes(vault)
        m = load_manifest() or adopt_legacy(vault, notes, {})
        m = plan(vault, notes, m, ask_model)
        save_manifest(m)
        active = _active(m)
        covered = sum(len(h["members"]) for h in active.values())
        print(f"{len(active)} hubs covering {covered} of {len(notes)} notes; "
              f"{len(needs_writing(m))} to write, {len(needs_superseded_page(m))} old pages to redirect")
        for name in sorted(active)[:400]:
            print(f"  {len(active[name]['members']):3d}  {name}")
        return 0

    written = update(vault, args.max or int(cfg["hubs"]["max_per_run"]), args.workers, cfg=cfg)
    _last_run_path().parent.mkdir(parents=True, exist_ok=True)
    _last_run_path().write_text(dt.date.today().isoformat(), encoding="utf-8")
    print(f"wrote {len(written)} hub(s)")
    return 0
