import json
from pathlib import Path

import pytest

from afterwatch import claude_cli
from afterwatch.claude_cli import (
    AuthFailure,
    CliError,
    DailyCapReached,
    QuotaExceeded,
    record_attempt,
    run_claude,
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Never touch the real ~/.afterwatch state file."""
    state = tmp_path / "cli-attempts.json"
    monkeypatch.setattr(claude_cli, "attempt_state_path", lambda: state)
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: None)  # no real waiting
    return state


def test_record_attempt_increments_and_resets(_isolated_state):
    assert record_attempt(today="2026-08-01") == 1
    assert record_attempt(today="2026-08-01") == 2
    assert record_attempt(today="2026-08-02") == 1  # new day resets


@pytest.mark.parametrize("corrupt", ["42", "[]", "null", '"a string"', "{oh no", ""])
def test_record_attempt_survives_corrupt_state_file(_isolated_state, corrupt):
    """Counting is best-effort: a torn write must never crash the pipeline."""
    _isolated_state.write_text(corrupt, encoding="utf-8")
    assert record_attempt(today="2026-08-01") == 1  # starts fresh, no exception


def test_run_claude_returns_on_first_success(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli, "invoke_once", lambda p, **kw: "note text")
    assert run_claude("p", cwd=tmp_path) == "note text"


def test_auth_failure_is_not_retried(tmp_path, monkeypatch):
    calls = []

    def fail(p, **kw):
        calls.append(1)
        raise AuthFailure("not logged in")

    monkeypatch.setattr(claude_cli, "invoke_once", fail)
    with pytest.raises(AuthFailure):
        run_claude("p", cwd=tmp_path)
    assert len(calls) == 1  # exactly one attempt


def test_transient_error_retries_then_gives_up(tmp_path, monkeypatch):
    calls = []

    def fail(p, **kw):
        calls.append(1)
        raise CliError("boom")

    monkeypatch.setattr(claude_cli, "invoke_once", fail)
    with pytest.raises(CliError):
        run_claude("p", cwd=tmp_path)
    assert len(calls) == claude_cli.MAX_TRANSIENT_ATTEMPTS


def test_transient_error_then_success(tmp_path, monkeypatch):
    state = {"n": 0}

    def flaky(p, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise CliError("transient")
        return "recovered"

    monkeypatch.setattr(claude_cli, "invoke_once", flaky)
    assert run_claude("p", cwd=tmp_path) == "recovered"


def test_quota_backs_off_then_succeeds(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: slept.append(s))
    state = {"n": 0}

    def quota_twice(p, **kw):
        state["n"] += 1
        if state["n"] <= 2:
            raise QuotaExceeded("usage limit reached")
        return "after quota"

    monkeypatch.setattr(claude_cli, "invoke_once", quota_twice)
    assert run_claude("p", cwd=tmp_path) == "after quota"
    assert slept == [claude_cli.QUOTA_BACKOFF_START,
                     claude_cli.QUOTA_BACKOFF_START * 2]  # doubling


def test_quota_gives_up_after_max_total_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli.time, "sleep", lambda s: None)

    def always_quota(p, **kw):
        raise QuotaExceeded("usage limit reached")

    monkeypatch.setattr(claude_cli, "invoke_once", always_quota)
    with pytest.raises(QuotaExceeded):
        run_claude("p", cwd=tmp_path)


def test_daily_cap_blocks_further_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli, "invoke_once", lambda p, **kw: "ok")
    monkeypatch.setattr(claude_cli, "DAILY_ATTEMPT_CAP", 2)
    assert run_claude("p", cwd=tmp_path) == "ok"
    assert run_claude("p", cwd=tmp_path) == "ok"
    with pytest.raises(DailyCapReached):
        run_claude("p", cwd=tmp_path)


# --- the counter is shared by the bot and parallel backfill runners ---

def _bump(path_str, n):
    from afterwatch import claude_cli as cc
    cc.attempt_state_path = lambda: Path(path_str)
    for _ in range(n):
        cc.record_attempt(today="2026-08-01")


def test_record_attempt_counts_every_call_across_processes(_isolated_state):
    """Unlocked, a reader could catch the file mid-write, read it as empty and
    restart the day at 0 (17.10.: 599 calls counted as 197)."""
    import multiprocessing as mp
    procs = [mp.Process(target=_bump, args=(str(_isolated_state), 250)) for _ in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert json.loads(_isolated_state.read_text(encoding="utf-8"))["attempts"] == 2000


def test_attempts_today_reads_without_counting(_isolated_state):
    assert claude_cli.attempts_today(today="2026-08-01") == 0
    record_attempt(today="2026-08-01")
    record_attempt(today="2026-08-01")
    assert claude_cli.attempts_today(today="2026-08-01") == 2
    assert claude_cli.attempts_today(today="2026-08-02") == 0  # other day
