"""Turn a YouTube watch history into notes in bounded, checkable steps.

    afterwatch backfill import <takeout.zip | watch-history.html | watch-history.json>
    afterwatch backfill enrich [--limit N]     # duration, category, channel, description
    afterwatch backfill judge [--limit N]      # model: worth a structured note? (N = batches)
    afterwatch backfill report                 # counts per stage + an audit sample to check
    afterwatch backfill summarise [--limit N] [--part K/N] [--model M] [--until-done] [--yes]

Everything is cached under <state dir>/backfill, so every step resumes where it
stopped. Enrichment uses the YouTube Data API with a plain API key when one is
set (YOUTUBE_API_KEY, or the stored "youtube" key), otherwise yt-dlp per video.
No YouTube login or cookies are involved anywhere.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import claude_cli, config, pipeline
from .llm import AuthFailure, ClaudeCliError, DailyCapReached, QuotaExceeded, complete

API_URL = "https://www.googleapis.com/youtube/v3/videos"
API_BATCH = 50
YTDLP_DELAY = 1.5       # seconds between per-video yt-dlp lookups
JUDGE_BATCH = 25
MUSIC_CATEGORY = "10"

# Reasons are matched in lower case. Throttling must stop a run, never mark a
# video dead; a dead video is one a retry cannot fix.
THROTTLED = ("too many requests", "http error 429", "not a bot", "blocking requests",
             "ipblocked", "requestblocked")
QUOTA = ("usage limit", "quota", "rate limit", "overload")
AUTH = ("not logged in", "api key", "authenticat", "credentials")
PERMANENT = ("no transcript obtainable", "video inaccessible")
NETWORK = ("urlopen error", "timed out", "connection", "network is unreachable")

STOP_TEXT = {
    "cap": "daily call cap reached; rerun after local midnight",
    "quota": "usage limit hit; rerun later",
    "auth": "authentication problem; check the model or API key setup",
    "throttled": "YouTube is throttling requests; wait a while (or change network) and rerun",
    "midnight": "midnight passed; the daily cap restarts",
    "limit": "--limit reached",
    "errors": "the model failed three batches in a row; check the model setup",
}

RULES = {
    "aggressive": "Keep only videos that are clearly informative: video essays, analysis, "
                  "explainers, deep dives, documentaries, critique, reviews, news analysis, "
                  "lectures, interviews with substance. Drop clips, compilations, streams, "
                  "gameplay, reactions, skits, memes, vlogs, challenges, music, and anything "
                  "you cannot tell is informative. When unsure, drop.",
    "balanced": "Keep videos with real informational or analytical content, including "
                "longer interviews and talks. Drop clips, compilations, streams, gameplay, "
                "reactions, skits, memes and music. When unsure, judge by the description.",
    "lenient": "Drop only obvious entertainment: clips, compilations, streams, gameplay, "
               "skits, memes and music videos. Keep everything else. When unsure, keep.",
}


class BackfillError(Exception):
    """A problem the user can act on; main() prints it and exits with 2."""


class Throttled(BackfillError):
    pass


# --- state ---

def _path(name: str) -> Path:
    return config.state_dir() / "backfill" / name


def load_state(name: str, default):
    try:
        return json.loads(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_state(name: str, data) -> None:
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


# --- Takeout import ---

_HTML_ENTRY = re.compile(
    r'<a href="https://www\.youtube\.com/(watch\?v=|shorts/)([A-Za-z0-9_-]{11})[^"]*">(.*?)</a>'
    r'(?:<br>\s*<a href="https://www\.youtube\.com/channel/[^"]+">(.*?)</a>)?', re.S)


def _add(out: dict, vid: str, title: str, channel: str, short: bool) -> None:
    v = out.setdefault(vid, {"title": title, "channel": channel, "watch_count": 0, "short": short})
    v["watch_count"] += 1


def _parse_html(text: str) -> dict:
    out: dict = {}
    # One cell per history entry. Ads link to the advertiser's video, so they
    # are dropped by their label; removed videos have no link and never match.
    for block in text.split('<div class="outer-cell')[1:]:
        if "From Google Ads" in block:
            continue
        m = _HTML_ENTRY.search(block)
        if m:
            kind, vid, title, channel = m.groups()
            _add(out, vid, html.unescape(title), html.unescape(channel or "").strip(),
                 kind == "shorts/")
    return out


def _parse_json(text: str) -> dict:
    out: dict = {}
    for e in json.loads(text):
        url = e.get("titleUrl") or ""
        ids = pipeline.find_video_ids(url)
        if not ids or any(d.get("name") == "From Google Ads" for d in e.get("details") or []):
            continue
        title = re.sub(r"^Watched\s+", "", e.get("title") or "")
        channel = ((e.get("subtitles") or [{}])[0].get("name") or "").strip()
        _add(out, ids[0], title, channel, "/shorts/" in url)
    return out


def parse_takeout(path: Path) -> dict:
    """video id -> {title, channel, watch_count, short} from a Takeout zip or
    from the watch-history file inside it."""
    path = Path(path)
    if not path.is_file():
        raise BackfillError(f"not a file: {path}")
    suffix = path.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            names = {Path(n).name.lower(): n for n in z.namelist()}
            # Takeout has shipped both spellings.
            name = next((names[n] for n in ("watch-history.json", "watch_history.json",
                                            "watch-history.html", "watch_history.html")
                         if n in names), None)
            if not name:
                raise BackfillError("no watch-history.json or watch-history.html in that zip "
                                    "(export YouTube > history from Google Takeout)")
            text, suffix = z.read(name).decode("utf-8", "replace"), Path(name).suffix.lower()
    elif suffix in (".json", ".html"):
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        raise BackfillError("expected a .zip, .html or .json file")
    try:
        return _parse_json(text) if suffix == ".json" else _parse_html(text)
    except (ValueError, AttributeError) as exc:
        raise BackfillError(f"could not read the watch history: {exc}") from exc


# --- enrichment ---

def iso_seconds(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    d, h, mi, sec = (int(x or 0) for x in m.groups()) if m else (0, 0, 0, 0)
    return ((d * 24 + h) * 60 + mi) * 60 + sec


def fetch_api(ids: list[str], key: str) -> dict:
    """videos.list for up to 50 ids. Ids the API does not return map to None
    (deleted or private)."""
    query = urllib.parse.urlencode({"part": "snippet,contentDetails", "id": ",".join(ids),
                                    "maxResults": API_BATCH, "key": key})
    try:
        with urllib.request.urlopen(f"{API_URL}?{query}", timeout=30) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("items", [])
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise BackfillError(f"YouTube API answered {exc.code} (key invalid or daily quota "
                            f"used up): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BackfillError(f"YouTube API not reachable: {exc}") from exc
    found = {}
    for it in items:
        sn = it.get("snippet", {})
        found[it["id"]] = {
            "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
            "duration": iso_seconds(it.get("contentDetails", {}).get("duration", "")),
            "category": str(sn.get("categoryId", "")),
            "description": (sn.get("description") or "")[:600]}
    return {i: found.get(i) for i in ids}


def fetch_ytdlp(vid: str) -> dict | None:
    import yt_dlp
    from .frames import YDL_JS
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "js_runtimes": YDL_JS}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        if any(t in msg for t in THROTTLED):
            raise Throttled("YouTube is throttling yt-dlp; wait a while and rerun "
                            "(progress is saved)") from exc
        if any(t in msg for t in NETWORK):
            raise BackfillError(f"network problem, stopping (progress is saved): {exc}") from exc
        return None  # private, removed, age-restricted, ...
    cats = info.get("categories") or []
    return {"title": info.get("title") or "",
            "channel": info.get("channel") or info.get("uploader") or "",
            "duration": int(info.get("duration") or 0),
            "category": MUSIC_CATEGORY if "Music" in cats else (cats[0] if cats else ""),
            "description": (info.get("description") or "")[:600],
            "short": "/shorts/" in (info.get("webpage_url") or "")}


def enrich(ids: list[str], key: str | None, log=print) -> str | None:
    """Fetch and cache details for ids not cached yet. Returns why it stopped
    early, or None when all were done. Progress is saved either way."""
    cache = load_state("enriched.json", {})
    todo = [i for i in ids if i not in cache]
    stop = None
    try:
        if key:
            for n in range(0, len(todo), API_BATCH):
                cache.update(fetch_api(todo[n:n + API_BATCH], key))
                log(f"  enriched {min(n + API_BATCH, len(todo))}/{len(todo)}")
                if (n // API_BATCH) % 20 == 19:
                    save_state("enriched.json", cache)
        else:
            for n, vid in enumerate(todo):
                if n:
                    time.sleep(YTDLP_DELAY)
                cache[vid] = fetch_ytdlp(vid)
                if n % 25 == 24:
                    save_state("enriched.json", cache)
                    log(f"  enriched {n + 1}/{len(todo)}")
    except BackfillError as exc:
        stop = str(exc)
    finally:
        save_state("enriched.json", cache)
    return stop


# --- cuts ---

def noted_ids(vault: Path, cfg: dict) -> set[str]:
    ids: set[str] = set()
    for p in (vault / config.folder("notes", cfg)).glob("*.md"):
        try:
            ids.update(pipeline.find_video_ids(p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return ids


@dataclass
class Stages:
    history: dict
    cache: dict
    cuts: dict = field(default_factory=dict)     # video id -> why it was cut
    passed: list = field(default_factory=list)   # enriched and not cut
    pending: list = field(default_factory=list)  # not cut, not enriched yet


def classify(cfg: dict, vault: Path) -> Stages:
    history = load_state("history.json", {})
    if not history:
        raise BackfillError("nothing imported yet: run `afterwatch backfill import <takeout>` first")
    st = Stages(history, load_state("enriched.json", {}))
    b = cfg["backfill"]
    noted = noted_ids(vault, cfg)
    drop = {c.strip().lower() for c in b["drop_channels"]}
    for vid, h in history.items():
        meta = st.cache.get(vid)
        if vid in noted:
            reason = "already has a note"
        elif h["channel"].lower() in drop:
            reason = "dropped channel"
        elif h.get("short"):
            reason = "YouTube Short"
        elif vid not in st.cache:
            st.pending.append(vid)
            continue
        elif meta is None:
            reason = "deleted or private"
        elif meta.get("short"):
            reason = "YouTube Short"
        elif meta["channel"].lower() in drop:
            reason = "dropped channel"
        elif meta["category"] == MUSIC_CATEGORY:
            reason = "music category"
        elif meta["duration"] < b["min_minutes"] * 60:
            reason = f"under {b['min_minutes']:g} minutes"
        else:
            reason = None
        if reason:
            st.cuts[vid] = reason
        else:
            st.passed.append(vid)
    return st


def keepers(cfg: dict, st: Stages) -> list[str]:
    """Videos that passed the rules (and the judge, when it is on), most
    rewatched first so stopping early keeps the strongest picks."""
    verdicts = load_state("verdicts.json", {})
    ids = [v for v in st.passed if verdicts.get(v) == "keep"] if cfg["backfill"]["judge"] \
        else st.passed
    return sorted(ids, key=lambda v: -st.history[v]["watch_count"])  # stable on ties


# --- judge ---

def parse_keep(reply: str, n: int) -> set[int] | None:
    """The numbers (1..n) in the first JSON array of numbers in the reply, or
    None when there is no such array. Numbers outside 1..n are ignored."""
    for m in re.finditer(r"\[[^\[\]]*\]", reply):
        try:
            arr = json.loads(m.group(0))
        except ValueError:
            continue
        if all(isinstance(x, (int, str)) and str(x).strip().isdigit() for x in arr):
            return {int(x) for x in arr if 1 <= int(x) <= n}
    return None


def judge_prompt(rows: list[tuple[str, str, int, str]], strictness: str) -> str:
    lines = [f"{n} | {t} | {c} | {mins} | {' '.join(d.split())[:200]}"
             for n, (t, c, mins, d) in enumerate(rows, 1)]
    return ("You are filtering a YouTube watch history down to the videos worth a structured "
            "written note (arguments, facts, takeaways).\n" + RULES[strictness] + "\n\n"
            "Each line: number | title | channel | minutes | start of description.\n"
            "Answer with ONLY a JSON array of the numbers to KEEP, e.g. [2, 7, 31]. "
            "Nothing else.\n\n" + "\n".join(lines))


def judge(cfg: dict, st: Stages, limit: int | None = None, log=print) -> str | None:
    """Judge passed videos in batches; verdicts are saved after every batch.
    Returns a stop reason key, or None."""
    strictness = cfg["backfill"]["strictness"]
    if strictness not in RULES:
        raise BackfillError(f"backfill.strictness must be one of {', '.join(RULES)}")
    verdicts = load_state("verdicts.json", {})
    todo = [v for v in st.passed if v not in verdicts]
    batches = [todo[i:i + JUDGE_BATCH] for i in range(0, len(todo), JUDGE_BATCH)][:limit]
    log(f"{len(todo)} videos to judge, {len(batches)} batch(es) of up to {JUDGE_BATCH}")
    failures = 0
    for b, batch in enumerate(batches, 1):
        rows = [(st.history[v]["title"], st.history[v]["channel"],
                 st.cache[v]["duration"] // 60, st.cache[v]["description"]) for v in batch]
        try:
            reply = complete("judge", judge_prompt(rows, strictness), cfg=cfg)
        except DailyCapReached:
            return "cap"
        except QuotaExceeded:
            return "quota"
        except AuthFailure:
            return "auth"
        except ClaudeCliError as exc:
            failures += 1
            log(f"  batch {b}: model call failed ({exc}); a rerun retries it")
            if failures >= 3:
                return "errors"
            continue
        failures = 0
        keep = parse_keep(reply, len(batch))
        if keep is None:
            log(f"  batch {b}: no JSON array in the answer, skipped (a rerun retries it)")
            continue
        verdicts.update({v: "keep" if n in keep else "drop" for n, v in enumerate(batch, 1)})
        save_state("verdicts.json", verdicts)
        log(f"  batch {b}/{len(batches)}: kept {len(keep)} of {len(batch)}")
    return None


# --- report ---

def _line(st: Stages, vid: str) -> str:
    h, meta = st.history[vid], st.cache.get(vid) or {}
    mins = f"{meta['duration'] // 60}m" if "duration" in meta else "?"
    return f"    - {h['title'][:62]}  [{h['channel'][:22]}, {mins}]"


def build_report(cfg: dict, st: Stages, rng: random.Random) -> str:
    verdicts = load_state("verdicts.json", {})
    out = [f"Imported: {len(st.history)} distinct videos",
           f"Cut by rules: {len(st.cuts)}"]
    by_reason: dict[str, list] = {}
    for vid, reason in st.cuts.items():
        by_reason.setdefault(reason, []).append(vid)
    for reason, ids in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        out.append(f"  {len(ids):6d}  {reason}")
        out += [_line(st, v) for v in rng.sample(ids, min(4, len(ids)))]
    if st.pending:
        out.append(f"Not enriched yet: {len(st.pending)} (run `afterwatch backfill enrich`)")
    out.append(f"Passed the rules: {len(st.passed)}")
    if cfg["backfill"]["judge"]:
        groups = {"judge: keep": 12, "judge: no": 8, "not judged yet": 8}
        ids_of = {"judge: keep": [v for v in st.passed if verdicts.get(v) == "keep"],
                  "judge: no": [v for v in st.passed if verdicts.get(v) == "drop"],
                  "not judged yet": [v for v in st.passed if v not in verdicts]}
    else:
        groups, ids_of = {"kept": 20}, {"kept": st.passed}
    for name, sample in groups.items():
        ids = ids_of[name]
        out.append(f"  {len(ids):6d}  {name}")
        out += [_line(st, v) for v in rng.sample(ids, min(sample, len(ids)))]
    return "\n".join(out)


def write_cuts_csv(st: Stages) -> Path:
    path = _path("cuts.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "title", "channel", "reason"])
        for vid, reason in st.cuts.items():
            w.writerow([vid, st.history[vid]["title"], st.history[vid]["channel"], reason])
    return path


# --- summarise ---

_today = date.today  # a name tests can replace


def stop_for(text: str) -> str | None:
    """Which stop condition a failure message means, if any."""
    t = text.lower()
    if any(m in t for m in THROTTLED):
        return "throttled"
    if "daily cli attempt cap" in t:
        return "cap"
    if any(m in t for m in QUOTA):
        return "quota"
    if any(m in t for m in AUTH):
        return "auth"
    return None


@dataclass
class Run:
    created: int = 0
    failed: int = 0
    duplicates: int = 0
    dead: int = 0       # skipped as known-unfixable
    stop: str | None = None


def _todo(cfg: dict, vault: Path, part: tuple[int, int] | None) -> tuple[list[str], int, Stages]:
    st = classify(cfg, vault)
    dead = load_state("dead.json", {})
    ids = keepers(cfg, st)
    todo = [v for v in ids if v not in dead]
    dead_skipped = len(ids) - len(todo)
    if part:
        todo = todo[part[0] - 1::part[1]]
    return todo, dead_skipped, st


def parse_part(s: str) -> tuple[int, int]:
    try:
        k, n = (int(x) for x in s.split("/"))
    except ValueError:
        raise BackfillError("--part must look like K/N, e.g. 1/2") from None
    if not 1 <= k <= n:
        raise BackfillError("--part K/N needs 1 <= K <= N")
    return k, n


def summarise(cfg: dict, vault: Path, *, limit: int | None = None,
              part: tuple[int, int] | None = None, model: str | None = None,
              log=print) -> Run:
    todo, dead_skipped, st = _todo(cfg, vault, part)
    run = Run(dead=dead_skipped)
    dead = load_state("dead.json", {})
    cap = int(cfg["limits"]["daily_calls"])
    started_on = _today()
    for i, vid in enumerate(todo, 1):
        if _today() != started_on:
            run.stop = "midnight"  # the call counter restarts at midnight
            break
        if limit and run.created >= limit:
            run.stop = "limit"
            break
        # Two spare calls: distilling may retry once. Without this check the
        # cap error would land in the failed-videos note for every video.
        if claude_cli.attempts_today() >= cap - 2:
            run.stop = "cap"
            break
        try:
            result = pipeline.process_one(pipeline.parse_line(f"https://www.youtube.com/watch?v={vid}"),
                                          vault, model=model)
        except DailyCapReached:
            run.stop = "cap"
            break
        except QuotaExceeded:
            run.stop = "quota"
            break
        except AuthFailure:
            run.stop = "auth"
            break
        if result.status == "duplicate":
            run.duplicates += 1
            continue
        log(f"[{i}/{len(todo)}] {result.status}: {st.history[vid]['title'][:70]}")
        if result.status == "created":
            run.created += 1
            continue
        run.failed += 1
        reason = result.reason or ""
        log(f"    {reason}")
        run.stop = stop_for(reason)
        if run.stop:
            break
        if any(p in reason.lower() for p in PERMANENT):
            dead[vid] = reason
            save_state("dead.json", dead)
    return run


def until_done(cfg: dict, vault: Path, *, limit: int | None, part, model, log=print) -> Run:
    """Rerun summarise until no keeper is left without a note, or something
    stops it. A round without a new note also stops it, so videos that keep
    failing cannot make it spin."""
    total = Run()
    while True:
        if limit is not None and total.created >= limit:
            total.stop = "limit"
            return total
        run = summarise(cfg, vault, part=part, model=model, log=log,
                        limit=None if limit is None else limit - total.created)
        for f in ("created", "failed", "duplicates", "dead"):
            setattr(total, f, getattr(total, f) + getattr(run, f))
        total.stop = run.stop
        if run.stop not in (None, "midnight"):
            return total
        if not _todo(cfg, vault, part)[0]:
            total.stop = None
            return total
        if run.created == 0 and run.stop is None:
            log("no new note this round; stopping so failing videos cannot loop")
            return total


# --- command line ---

def resolve_vault(flag: str | None, cfg: dict) -> Path:
    from .cli import resolve_output
    try:
        return resolve_output(flag, cfg)
    except config.ConfigError as exc:
        raise BackfillError(str(exc)) from exc


def _api_key(cfg: dict) -> str | None:
    return os.environ.get("YOUTUBE_API_KEY") or config.api_key("youtube", cfg)


def _stopped(run_stop: str | None, say=print) -> int:
    if run_stop:
        say(f"Stopped: {STOP_TEXT.get(run_stop, run_stop)}.")
    return 1 if run_stop == "auth" else 0


def cmd_import(args, cfg) -> int:
    parsed = parse_takeout(Path(args.file))
    history = load_state("history.json", {})
    new = len(parsed.keys() - history.keys())
    history.update(parsed)  # overwrite, so importing the same export twice never doubles counts
    save_state("history.json", history)
    print(f"{len(parsed)} distinct videos in that export ({new} new); {len(history)} in total")
    return 0


def cmd_enrich(args, cfg) -> int:
    st = classify(cfg, resolve_vault(args.out, cfg))
    todo = st.pending[:args.limit]
    key = _api_key(cfg)
    print(f"{len(todo)} videos to look up via " + ("the YouTube API" if key else
          f"yt-dlp (about {len(todo) * YTDLP_DELAY / 60:.0f} min; set YOUTUBE_API_KEY for speed)"))
    stop = enrich(todo, key)
    if stop:
        print(f"Stopped: {stop}")
        return 1
    return 0


def cmd_judge(args, cfg) -> int:
    if not cfg["backfill"]["judge"]:
        print("The judge is off (backfill.judge = false); every video that passes the rules "
              "counts as a keeper.")
        return 0
    st = classify(cfg, resolve_vault(args.out, cfg))
    if st.pending:
        print(f"Note: {len(st.pending)} videos are not enriched yet and are skipped.")
    return _stopped(judge(cfg, st, args.limit))


def cmd_report(args, cfg) -> int:
    st = classify(cfg, resolve_vault(args.out, cfg))
    print(build_report(cfg, st, random.Random(17)))
    print(f"\nAll cuts with reasons: {write_cuts_csv(st)}")
    save_state("report.json", {"at": datetime.now().isoformat(timespec="seconds")})
    return 0


def cmd_summarise(args, cfg) -> int:
    vault = resolve_vault(args.out, cfg)
    if not args.yes and not load_state("report.json", {}).get("at"):
        print("Run `afterwatch backfill report` first and check the sample, or pass --yes.",
              file=sys.stderr)
        return 2
    part = parse_part(args.part) if args.part else None
    todo, dead, st = _todo(cfg, vault, part)
    unjudged = len([v for v in st.passed if v not in load_state("verdicts.json", {})])
    if cfg["backfill"]["judge"] and unjudged:
        print(f"Note: {unjudged} videos are not judged yet and are left out "
              "(`afterwatch backfill judge`).")
    print(f"About to summarise {min(len(todo), args.limit or len(todo))} of {len(todo)} "
          f"videos without a note ({dead} known-unfixable skipped).")
    if args.until_done:
        run = until_done(cfg, vault, limit=args.limit, part=part, model=args.model)
    else:
        run = summarise(cfg, vault, limit=args.limit, part=part, model=args.model)
    left = len(_todo(cfg, vault, part)[0])
    print(f"\ncreated {run.created}, already done {run.duplicates}, failed {run.failed}, "
          f"known-unfixable {run.dead}; {left} still without a note")
    return _stopped(run.stop)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--out", help="vault or notes folder (default: output.path)")
    ap = argparse.ArgumentParser(prog="afterwatch backfill", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", parents=[common], help="read a Takeout watch history"
                   ).add_argument("file", help=".zip, .html or .json")
    sub.add_parser("enrich", parents=[common], help="look up duration, category, description"
                   ).add_argument("--limit", type=int, help="look up at most this many videos")
    sub.add_parser("judge", parents=[common], help="model decides which videos deserve a note"
                   ).add_argument("--limit", type=int, help="run at most this many batches")
    sub.add_parser("report", parents=[common], help="counts per stage and an audit sample")
    s = sub.add_parser("summarise", parents=[common], help="write notes for the keepers")
    s.add_argument("--limit", type=int, help="stop after this many new notes")
    s.add_argument("--part", metavar="K/N", help="take every Nth video from the Kth, so N "
                   "copies can run in parallel")
    s.add_argument("--model", help="model name for the notes (overrides the configured one)")
    s.add_argument("--until-done", action="store_true",
                   help="rerun until every keeper has a note or something stops it")
    s.add_argument("--yes", action="store_true", help="skip the report check")
    args = ap.parse_args(argv)
    cmd = {"import": cmd_import, "enrich": cmd_enrich, "judge": cmd_judge,
           "report": cmd_report, "summarise": cmd_summarise}[args.cmd]
    try:
        return cmd(args, config.load())
    except (BackfillError, config.ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
