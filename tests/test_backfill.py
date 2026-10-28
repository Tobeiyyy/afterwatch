from __future__ import annotations

import io
import json
import random
import urllib.error
import zipfile
from datetime import date
from pathlib import Path

import pytest

from afterwatch import backfill as bf
from afterwatch import claude_cli, config, pipeline
from afterwatch.llm import AuthFailure, DailyCapReached, QuotaExceeded
from afterwatch.pipeline import Result


# --- fixtures ---

def vid(n: int) -> str:
    return f"vid{n:08d}"


def cell(inner: str) -> str:
    return (f'<div class="outer-cell mdl-cell"><div class="content-cell"><p>{inner}</p>'
            "</div></div>")


def watched(video_id: str, title: str, channel: str | None, kind="watch?v=") -> str:
    chan = f'<br><a href="https://www.youtube.com/channel/UC1">{channel}</a>' if channel else ""
    return cell(f'Watched&nbsp;<a href="https://www.youtube.com/{kind}{video_id}">{title}</a>'
                f"{chan}<br>Jan 1, 2026")


HTML = "<html><body>" + "".join([
    watched("aaaaaaaaaaa", "First &amp; Best", "Chan One"),
    watched("aaaaaaaaaaa", "First &amp; Best", "Chan One"),
    watched("bbbbbbbbbbb", "Second", "Chan Two"),
    watched("sssssssssss", "A short", "Chan Two", kind="shorts/"),
    cell('Watched&nbsp;<a href="https://www.youtube.com/watch?v=adadadadada">Buy now</a>'
         "<br>From Google Ads"),
    cell("Watched a video that has been removed"),
    cell('Searched for <a href="https://www.youtube.com/results?search_query=x">x</a>'),
]) + "</body></html>"

JSON = json.dumps([
    {"title": "Watched First & Best", "titleUrl": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
     "subtitles": [{"name": "Chan One", "url": "u"}]},
    {"title": "Watched First & Best", "titleUrl": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
     "subtitles": [{"name": "Chan One", "url": "u"}]},
    {"title": "Watched Second", "titleUrl": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
     "subtitles": [{"name": "Chan Two", "url": "u"}]},
    {"title": "Watched A short", "titleUrl": "https://www.youtube.com/shorts/sssssssssss"},
    {"title": "Watched Buy now", "titleUrl": "https://www.youtube.com/watch?v=adadadadada",
     "details": [{"name": "From Google Ads"}]},
    {"title": "Watched a video that has been removed"},
])


def check_parsed(out: dict):
    assert set(out) == {"aaaaaaaaaaa", "bbbbbbbbbbb", "sssssssssss"}
    assert out["aaaaaaaaaaa"] == {"title": "First & Best", "channel": "Chan One",
                                  "watch_count": 2, "short": False}
    assert out["bbbbbbbbbbb"]["watch_count"] == 1
    assert out["sssssssssss"]["short"] is True


def make_zip(path: Path, **members: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, text in members.items():
            z.writestr(name, text)
    return path


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "Videos").mkdir(parents=True)
    return v


@pytest.fixture
def cfg():
    c = config.load()
    c["backfill"]["judge"] = False
    return c


def meta(minutes=10, channel="Some Channel", category="27", description="about things", **kw):
    return {"title": "t", "channel": channel, "duration": minutes * 60, "category": category,
            "description": description, **kw}


def seed(n: int, watch=None, **meta_kw) -> list[str]:
    """n imported + enriched videos vid(1)..vid(n)."""
    ids = [vid(i) for i in range(1, n + 1)]
    bf.save_state("history.json", {v: {"title": f"Title {v}", "channel": f"Chan {i}",
                                       "watch_count": (watch or {}).get(v, 1), "short": False}
                                   for i, v in enumerate(ids)})
    bf.save_state("enriched.json", {v: meta(**meta_kw) for v in ids})
    return ids


def add_note(vault: Path, video_id: str):
    (vault / "Videos" / f"{video_id}.md").write_text(
        f"text\n\n[x](https://www.youtube.com/watch?v={video_id})\n", encoding="utf-8")


# --- Takeout parsing ---

def test_parse_html():
    check_parsed(bf._parse_html(HTML))


def test_parse_json():
    check_parsed(bf._parse_json(JSON))


def test_parse_files_and_zip(tmp_path):
    h = tmp_path / "watch-history.html"
    h.write_text(HTML, encoding="utf-8")
    check_parsed(bf.parse_takeout(h))
    j = tmp_path / "watch-history.json"
    j.write_text(JSON, encoding="utf-8")
    check_parsed(bf.parse_takeout(j))
    check_parsed(bf.parse_takeout(make_zip(
        tmp_path / "a.zip", **{"Takeout/YouTube/history/watch-history.html": HTML})))
    check_parsed(bf.parse_takeout(make_zip(
        tmp_path / "b.zip", **{"Takeout/YouTube/history/watch-history.json": JSON})))
    check_parsed(bf.parse_takeout(make_zip(
        tmp_path / "c.zip", **{"Takeout/YouTube/history/watch_history.json": JSON})))


def test_parse_empty_and_bad_input(tmp_path):
    empty = tmp_path / "watch-history.json"
    empty.write_text("[]", encoding="utf-8")
    assert bf.parse_takeout(empty) == {}
    with pytest.raises(bf.BackfillError, match="no watch-history"):
        bf.parse_takeout(make_zip(tmp_path / "c.zip", **{"other.txt": "x"}))
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(bf.BackfillError, match="expected a .zip"):
        bf.parse_takeout(tmp_path / "notes.txt")
    with pytest.raises(bf.BackfillError, match="not a file"):
        bf.parse_takeout(tmp_path / "missing.zip")
    broken = tmp_path / "broken" / "watch-history.json"
    broken.parent.mkdir()
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(bf.BackfillError, match="could not read"):
        bf.parse_takeout(broken)


def test_import_twice_does_not_double_counts(tmp_path, vault, capsys):
    j = tmp_path / "watch-history.json"
    j.write_text(JSON, encoding="utf-8")
    assert bf.main(["import", str(j)]) == 0
    assert bf.main(["import", str(j)]) == 0
    assert bf.load_state("history.json", {})["aaaaaaaaaaa"]["watch_count"] == 2
    assert "0 new" in capsys.readouterr().out


# --- cuts ---

def reasons(cfg, vault):
    return bf.classify(cfg, vault)


def test_classify_cut_reasons(cfg, vault):
    ids = seed(8)
    cache = bf.load_state("enriched.json", {})
    hist = bf.load_state("history.json", {})
    cache[ids[0]] = meta(minutes=4)                       # too short
    cache[ids[1]] = meta(category="10")                   # music
    cache[ids[2]] = None                                  # deleted
    cache[ids[3]] = meta(channel="Noisy Channel")         # dropped via enriched channel
    hist[ids[4]]["channel"] = "NOISY CHANNEL"             # dropped via history, any case
    hist[ids[5]]["short"] = True
    del cache[ids[6]]                                     # not enriched yet
    bf.save_state("enriched.json", cache)
    bf.save_state("history.json", hist)
    add_note(vault, ids[7])
    cfg["backfill"]["drop_channels"] = ["noisy channel"]
    st = bf.classify(cfg, vault)
    assert st.cuts == {ids[0]: "under 5 minutes", ids[1]: "music category",
                       ids[2]: "deleted or private", ids[3]: "dropped channel",
                       ids[4]: "dropped channel", ids[5]: "YouTube Short",
                       ids[7]: "already has a note"}
    assert st.pending == [ids[6]] and st.passed == []


def test_duration_boundary_and_min_minutes(cfg, vault):
    ids = seed(2)
    cache = bf.load_state("enriched.json", {})
    cache[ids[0]]["duration"] = 299
    cache[ids[1]]["duration"] = 300
    bf.save_state("enriched.json", cache)
    st = bf.classify(cfg, vault)
    assert st.cuts == {ids[0]: "under 5 minutes"} and st.passed == [ids[1]]
    cfg["backfill"]["min_minutes"] = 0
    assert bf.classify(cfg, vault).passed == ids


def test_classify_needs_an_import(cfg, vault):
    with pytest.raises(bf.BackfillError, match="nothing imported"):
        bf.classify(cfg, vault)


def test_keepers_follow_the_judge_and_rewatch_order(cfg, vault):
    ids = seed(4, watch={vid(3): 5, vid(2): 2})
    st = bf.classify(cfg, vault)
    assert bf.keepers(cfg, st) == [ids[2], ids[1], ids[0], ids[3]]      # judge off: all
    cfg["backfill"]["judge"] = True
    assert bf.keepers(cfg, st) == []                                    # nothing judged yet
    bf.save_state("verdicts.json", {ids[0]: "keep", ids[2]: "drop", ids[1]: "keep"})
    assert bf.keepers(cfg, st) == [ids[1], ids[0]]


# --- enrichment: API ---

class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def api_item(video_id, duration="PT10M5S", category="27"):
    return {"id": video_id, "snippet": {"title": "T " + video_id, "channelTitle": "C",
                                        "categoryId": category, "description": "d" * 700},
            "contentDetails": {"duration": duration}}


def test_iso_seconds():
    assert bf.iso_seconds("PT10M5S") == 605
    assert bf.iso_seconds("PT1H") == 3600
    assert bf.iso_seconds("P1DT1S") == 86401
    assert bf.iso_seconds("P0D") == 0 and bf.iso_seconds("") == 0 and bf.iso_seconds("junk") == 0


def test_api_enrich_batches_of_50_and_missing_videos(monkeypatch):
    ids = [vid(i) for i in range(1, 121)]
    urls = []

    def fake_urlopen(url, timeout=None):
        urls.append(url)
        asked = bf.urllib.parse.parse_qs(bf.urllib.parse.urlparse(url).query)["id"][0].split(",")
        return FakeResponse({"items": [api_item(i) for i in asked if i != vid(7)]})

    monkeypatch.setattr(bf.urllib.request, "urlopen", fake_urlopen)
    assert bf.enrich(ids, "KEY", log=lambda *_: None) is None
    sizes = [len(bf.urllib.parse.parse_qs(bf.urllib.parse.urlparse(u).query)["id"][0].split(","))
             for u in urls]
    assert sizes == [50, 50, 20]
    q = bf.urllib.parse.parse_qs(bf.urllib.parse.urlparse(urls[0]).query)
    assert q["key"] == ["KEY"] and q["part"] == ["snippet,contentDetails"]
    cache = bf.load_state("enriched.json", {})
    assert len(cache) == 120 and cache[vid(7)] is None
    assert cache[vid(1)]["duration"] == 605 and len(cache[vid(1)]["description"]) == 600
    # a rerun resumes: nothing cached is asked again
    urls.clear()
    bf.enrich(ids + [vid(500)], "KEY", log=lambda *_: None)
    assert len(urls) == 1 and vid(500) in urls[0]


def test_api_error_stops_and_keeps_earlier_batches(monkeypatch):
    ids = [vid(i) for i in range(1, 101)]
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        if len(calls) == 2:
            raise urllib.error.HTTPError(url, 403, "quota", {}, io.BytesIO(b"quotaExceeded"))
        return FakeResponse({"items": [api_item(i) for i in ids[:50]]})

    monkeypatch.setattr(bf.urllib.request, "urlopen", fake_urlopen)
    stop = bf.enrich(ids, "KEY", log=lambda *_: None)
    assert "403" in stop and "quotaExceeded" in stop and "KEY" not in stop
    assert set(bf.load_state("enriched.json", {})) == set(ids[:50])


# --- enrichment: yt-dlp fallback ---

class FakeYDL:
    outcomes: dict = {}

    def __init__(self, opts):
        assert opts["skip_download"] is True and "cookiesfrombrowser" not in opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        assert download is False
        out = self.outcomes[url.rsplit("=", 1)[1]]
        if isinstance(out, str):
            import yt_dlp
            raise yt_dlp.utils.DownloadError(out)
        return out


@pytest.fixture
def ydl(monkeypatch):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(bf, "YTDLP_DELAY", 0)
    FakeYDL.outcomes = {}
    return FakeYDL


def test_ytdlp_fallback_maps_fields(ydl):
    ydl.outcomes = {vid(1): {"title": "T", "channel": "C", "duration": 700, "categories": ["Music"],
                             "description": "d", "webpage_url": "https://www.youtube.com/shorts/x"},
                    vid(2): "ERROR: Video unavailable"}
    assert bf.enrich([vid(1), vid(2)], None, log=lambda *_: None) is None
    cache = bf.load_state("enriched.json", {})
    assert cache[vid(1)] == {"title": "T", "channel": "C", "duration": 700, "category": "10",
                             "description": "d", "short": True}
    assert cache[vid(2)] is None


def test_ytdlp_throttling_stops_without_caching_that_video(ydl):
    ydl.outcomes = {vid(1): {"title": "T", "duration": 600}, vid(2): "HTTP Error 429: Too Many Requests",
                    vid(3): {"title": "never asked"}}
    stop = bf.enrich([vid(1), vid(2), vid(3)], None, log=lambda *_: None)
    assert "throttling" in stop
    assert set(bf.load_state("enriched.json", {})) == {vid(1)}


def test_ytdlp_network_error_stops(ydl):
    ydl.outcomes = {vid(1): "<urlopen error timed out>"}
    assert "network" in bf.enrich([vid(1)], None, log=lambda *_: None)
    assert bf.load_state("enriched.json", {}) == {}


# --- judge ---

@pytest.mark.parametrize("reply,expected", [
    ("[2, 4]", {2, 4}),
    ("Sure!\n```json\n[1, 3]\n```", {1, 3}),
    ('{"keep": [2]}', {2}),
    ('["2", "4"]', {2, 4}),
    ("[]", set()),
    ("[1, 99, 0]", {1}),                       # outside the batch
    ("[\"a\"] then [3]", {3}),                 # first array is junk, second is usable
    ("I would keep the first one", None),
    ("[not json]", None),
    ("", None),
])
def test_parse_keep(reply, expected):
    assert bf.parse_keep(reply, 5) == expected


def judged_setup(cfg, vault, n):
    cfg["backfill"]["judge"] = True
    ids = seed(n)
    return ids, bf.classify(cfg, vault)


def test_judge_batches_and_partial_batch(cfg, vault, monkeypatch):
    ids, st = judged_setup(cfg, vault, 30)
    prompts = []

    def fake_complete(task, prompt, cfg=None):
        prompts.append(prompt)
        return "[1, 2, 99]" if len(prompts) == 1 else "[1]"

    monkeypatch.setattr(bf, "complete", fake_complete)
    assert bf.judge(cfg, st, log=lambda *_: None) is None
    assert len(prompts) == 2
    v = bf.load_state("verdicts.json", {})
    assert v[ids[0]] == "keep" and v[ids[1]] == "keep" and v[ids[2]] == "drop"
    assert v[ids[25]] == "keep" and v[ids[29]] == "drop"      # the 5-video last batch
    assert "Title " + ids[0] in prompts[0] and "Chan 0" in prompts[0] and "about things" in prompts[0]
    assert "| 10 |" in prompts[0]


def test_judge_junk_reply_skips_batch_and_rerun_retries(cfg, vault, monkeypatch):
    ids, st = judged_setup(cfg, vault, 30)
    replies = iter(["[3]", "no idea, sorry"])
    monkeypatch.setattr(bf, "complete", lambda *a, **k: next(replies))
    bf.judge(cfg, st, log=lambda *_: None)
    assert set(bf.load_state("verdicts.json", {})) == set(ids[:25])
    seen = []
    monkeypatch.setattr(bf, "complete", lambda t, p, cfg=None: seen.append(p) or "[1]")
    bf.judge(cfg, st, log=lambda *_: None)
    assert len(seen) == 1 and "Title " + ids[25] in seen[0]
    assert set(bf.load_state("verdicts.json", {})) == set(ids)


def test_judge_limit_counts_batches(cfg, vault, monkeypatch):
    ids, st = judged_setup(cfg, vault, 60)
    monkeypatch.setattr(bf, "complete", lambda *a, **k: "[1]")
    bf.judge(cfg, st, limit=1, log=lambda *_: None)
    assert len(bf.load_state("verdicts.json", {})) == 25


@pytest.mark.parametrize("exc,stop", [(DailyCapReached("x"), "cap"), (QuotaExceeded("x"), "quota"),
                                      (AuthFailure("x"), "auth")])
def test_judge_stops_cleanly_and_keeps_verdicts(cfg, vault, monkeypatch, exc, stop):
    ids, st = judged_setup(cfg, vault, 60)
    calls = []

    def fake(task, prompt, cfg=None):
        calls.append(1)
        if len(calls) == 2:
            raise exc
        return "[1]"

    monkeypatch.setattr(bf, "complete", fake)
    assert bf.judge(cfg, st, log=lambda *_: None) == stop
    assert len(bf.load_state("verdicts.json", {})) == 25


def test_judge_presets_differ_and_unknown_is_rejected(cfg, vault):
    rows = [("T", "C", 7, "desc")]
    aggressive = bf.judge_prompt(rows, "aggressive")
    lenient = bf.judge_prompt(rows, "lenient")
    assert "When unsure, drop" in aggressive and "When unsure, keep" in lenient
    assert bf.judge_prompt(rows, "balanced") not in (aggressive, lenient)
    ids, st = judged_setup(cfg, vault, 3)
    cfg["backfill"]["strictness"] = "harsh"
    with pytest.raises(bf.BackfillError, match="strictness"):
        bf.judge(cfg, st)


# --- report and its gate ---

def test_report_counts_and_audit_sample(cfg, vault):
    cfg["backfill"]["judge"] = True
    ids = seed(40)
    cache = bf.load_state("enriched.json", {})
    for i in ids[:6]:
        cache[i] = meta(minutes=1)
    bf.save_state("enriched.json", cache)
    bf.save_state("verdicts.json", {**{i: "keep" for i in ids[6:20]}, **{i: "drop" for i in ids[20:30]}})
    st = bf.classify(cfg, vault)
    text = bf.build_report(cfg, st, random.Random(1))
    assert "Imported: 40" in text and "Cut by rules: 6" in text
    assert "6  under 5 minutes" in text and "Passed the rules: 34" in text
    assert "14  judge: keep" in text and "10  judge: no" in text and "10  not judged yet" in text
    assert text.count("    - ") == 4 + 12 + 8 + 8        # sample sizes per group


def test_summarise_refuses_until_report_has_run(tmp_path, vault, cfg, monkeypatch, capsys):
    config.save(cfg)
    ids = seed(2)
    monkeypatch.setattr(pipeline, "process_one", lambda *a, **k: pytest.fail("must not run"))
    assert bf.main(["summarise", "--out", str(vault)]) == 2
    assert "report" in capsys.readouterr().err

    made = []
    monkeypatch.setattr(pipeline, "process_one",
                        lambda req, v, model=None: made.append(req.video_id) or
                        Result(url=req.url, status="created", video_id=req.video_id))
    assert bf.main(["report", "--out", str(vault)]) == 0
    assert (config.state_dir() / "backfill" / "cuts.csv").exists()
    assert bf.main(["summarise", "--out", str(vault)]) == 0
    out = capsys.readouterr().out
    assert "About to summarise 2 of 2" in out and made == ids


def test_summarise_yes_skips_the_gate(vault, cfg, monkeypatch, capsys):
    config.save(cfg)
    seed(1)
    monkeypatch.setattr(pipeline, "process_one",
                        lambda req, v, model=None: Result(url=req.url, status="created"))
    assert bf.main(["summarise", "--yes", "--out", str(vault)]) == 0


# --- summarise ---

class Script:
    """A process_one stand-in: outcome per video id, writes a note when 'created'."""

    def __init__(self, vault, outcomes=None, default="created"):
        self.vault, self.outcomes, self.default, self.calls, self.models = vault, outcomes or {}, default, [], []

    def __call__(self, req, vault, model=None):
        self.calls.append(req.video_id)
        self.models.append(model)
        out = self.outcomes.get(req.video_id, self.default)
        if isinstance(out, BaseException):
            raise out
        if out == "created":
            add_note(self.vault, req.video_id)
            return Result(url=req.url, status="created", video_id=req.video_id)
        if out == "duplicate":
            return Result(url=req.url, status="duplicate", video_id=req.video_id)
        return Result(url=req.url, status="failed", video_id=req.video_id, reason=out)


def run(cfg, vault, script, monkeypatch, **kw):
    monkeypatch.setattr(pipeline, "process_one", script)
    return bf.summarise(cfg, vault, log=lambda *_: None, **kw)


def test_summarise_creates_skips_duplicates_and_passes_model(cfg, vault, monkeypatch):
    ids = seed(4)
    s = Script(vault, {ids[1]: "duplicate"})
    r = run(cfg, vault, s, monkeypatch, model="opus")
    assert (r.created, r.duplicates, r.failed, r.stop) == (3, 1, 0, None)
    assert set(s.models) == {"opus"}


def test_summarise_most_rewatched_first_and_limit(cfg, vault, monkeypatch):
    ids = seed(4, watch={vid(4): 9, vid(3): 3})
    s = Script(vault)
    r = run(cfg, vault, s, monkeypatch, limit=2)
    assert s.calls == [ids[3], ids[2]] and r.created == 2 and r.stop == "limit"


@pytest.mark.parametrize("exc,stop", [(DailyCapReached("x"), "cap"), (QuotaExceeded("x"), "quota"),
                                      (AuthFailure("x"), "auth")])
def test_summarise_stops_on_raised_limits(cfg, vault, monkeypatch, exc, stop):
    ids = seed(3)
    s = Script(vault, {ids[1]: exc})
    r = run(cfg, vault, s, monkeypatch)
    assert r.stop == stop and s.calls == ids[:2] and r.created == 1


@pytest.mark.parametrize("reason,stop", [
    ("distillation failed: daily CLI attempt cap reached (200)", "cap"),
    ("distillation failed: claude usage limit reached", "quota"),
    ("distillation failed: not logged in", "auth"),
])
def test_summarise_stops_on_limit_text_in_failures(cfg, vault, monkeypatch, reason, stop):
    ids = seed(3)
    s = Script(vault, {ids[0]: reason})
    r = run(cfg, vault, s, monkeypatch)
    assert r.stop == stop and s.calls == ids[:1] and r.failed == 1
    assert bf.load_state("dead.json", {}) == {}


@pytest.mark.parametrize("reason", [
    "video inaccessible: ERROR: HTTP Error 429: Too Many Requests",
    "video inaccessible: Sign in to confirm you're not a bot",
    "no transcript obtainable: YouTube is blocking requests from your IP",
])
def test_throttled_video_stops_the_run_and_is_not_marked_dead(cfg, vault, monkeypatch, reason):
    ids = seed(3)
    s = Script(vault, {ids[0]: reason})
    r = run(cfg, vault, s, monkeypatch)
    assert r.stop == "throttled" and s.calls == ids[:1]
    assert bf.load_state("dead.json", {}) == {}
    s2 = Script(vault)                     # next run tries the same video again
    run(cfg, vault, s2, monkeypatch)
    assert s2.calls[0] == ids[0]


def test_dead_videos_are_recorded_and_skipped_next_time(cfg, vault, monkeypatch):
    ids = seed(3)
    s = Script(vault, {ids[1]: "no transcript obtainable: disabled", ids[2]: "video inaccessible: private"})
    r = run(cfg, vault, s, monkeypatch)
    assert (r.created, r.failed, r.stop) == (1, 2, None)
    assert set(bf.load_state("dead.json", {})) == {ids[1], ids[2]}
    s2 = Script(vault)
    r2 = run(cfg, vault, s2, monkeypatch)
    assert s2.calls == [] and r2.dead == 2


def test_other_failures_are_retried_not_dead(cfg, vault, monkeypatch):
    ids = seed(1)
    run(cfg, vault, Script(vault, {ids[0]: "distillation failed: boom"}), monkeypatch)
    assert bf.load_state("dead.json", {}) == {}
    s = Script(vault)
    assert run(cfg, vault, s, monkeypatch).created == 1 and s.calls == ids


def fake_days(monkeypatch, switch_after: int):
    n = []

    def today():
        n.append(1)
        return date(2026, 1, 1) if len(n) <= switch_after else date(2026, 1, 2)

    monkeypatch.setattr(bf, "_today", today)


def test_summarise_stops_at_midnight(cfg, vault, monkeypatch):
    ids = seed(3)
    fake_days(monkeypatch, switch_after=2)    # start, first video, then the next day
    s = Script(vault)
    r = run(cfg, vault, s, monkeypatch)
    assert r.stop == "midnight" and s.calls == ids[:1]


def test_summarise_stops_before_the_daily_cap(cfg, vault, monkeypatch):
    seed(2)
    monkeypatch.setattr(claude_cli, "attempts_today", lambda: cfg["limits"]["daily_calls"] - 2)
    s = Script(vault)
    r = run(cfg, vault, s, monkeypatch)
    assert r.stop == "cap" and s.calls == []


def test_part_splits_work_between_copies(cfg, vault, monkeypatch):
    ids = seed(5)
    a, b = Script(vault, default="duplicate"), Script(vault, default="duplicate")
    run(cfg, vault, a, monkeypatch, part=(1, 2))
    run(cfg, vault, b, monkeypatch, part=(2, 2))
    assert a.calls == [ids[0], ids[2], ids[4]] and b.calls == [ids[1], ids[3]]


def test_parse_part():
    assert bf.parse_part("2/3") == (2, 3)
    for bad in ("0/2", "3/2", "x", "1-2"):
        with pytest.raises(bf.BackfillError):
            bf.parse_part(bad)


def test_judged_out_videos_are_not_summarised(cfg, vault, monkeypatch):
    cfg["backfill"]["judge"] = True
    ids = seed(3)
    bf.save_state("verdicts.json", {ids[0]: "keep", ids[1]: "drop"})
    s = Script(vault)
    run(cfg, vault, s, monkeypatch)
    assert s.calls == [ids[0]]


# --- until done ---

def test_until_done_finishes_everything(cfg, vault, monkeypatch):
    seed(3)
    s = Script(vault)
    monkeypatch.setattr(pipeline, "process_one", s)
    r = bf.until_done(cfg, vault, limit=None, part=None, model=None, log=lambda *_: None)
    assert r.created == 3 and r.stop is None


def test_until_done_continues_after_midnight(cfg, vault, monkeypatch):
    seed(3)
    fake_days(monkeypatch, switch_after=2)
    s = Script(vault)
    monkeypatch.setattr(pipeline, "process_one", s)
    r = bf.until_done(cfg, vault, limit=None, part=None, model=None, log=lambda *_: None)
    assert r.created == 3 and r.stop is None and len(s.calls) == 3


def test_until_done_stops_when_a_round_makes_no_progress(cfg, vault, monkeypatch):
    ids = seed(3)
    s = Script(vault, {ids[1]: "distillation failed: boom"})
    monkeypatch.setattr(pipeline, "process_one", s)
    r = bf.until_done(cfg, vault, limit=None, part=None, model=None, log=lambda *_: None)
    assert r.created == 2 and s.calls.count(ids[1]) == 2      # tried again once, then gave up


def test_until_done_stops_on_a_hard_limit(cfg, vault, monkeypatch):
    ids = seed(3)
    s = Script(vault, {ids[1]: QuotaExceeded("x")})
    monkeypatch.setattr(pipeline, "process_one", s)
    r = bf.until_done(cfg, vault, limit=None, part=None, model=None, log=lambda *_: None)
    assert r.stop == "quota" and r.created == 1


def test_until_done_respects_limit(cfg, vault, monkeypatch):
    seed(5)
    s = Script(vault)
    monkeypatch.setattr(pipeline, "process_one", s)
    r = bf.until_done(cfg, vault, limit=2, part=None, model=None, log=lambda *_: None)
    assert r.created == 2 and r.stop == "limit"


# --- command line ---

def test_main_needs_a_vault(monkeypatch, capsys):
    assert bf.main(["report"]) == 2
    assert "no output folder" in capsys.readouterr().err


def test_main_enrich_uses_the_api_key_from_the_environment(vault, cfg, monkeypatch, capsys):
    ids = seed(2)
    bf.save_state("enriched.json", {})
    monkeypatch.setenv("YOUTUBE_API_KEY", "ENVKEY")
    seen = []
    monkeypatch.setattr(bf.urllib.request, "urlopen",
                        lambda url, timeout=None: seen.append(url) or FakeResponse(
                            {"items": [api_item(i) for i in ids]}))
    assert bf.main(["enrich", "--out", str(vault)]) == 0
    assert "key=ENVKEY" in seen[0] and "YouTube API" in capsys.readouterr().out
