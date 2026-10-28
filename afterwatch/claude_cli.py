"""Spawn the `claude` CLI (subscription-backed) instead of the paid SDK.

Only this module knows about the CLI. The prompt goes on stdin (transcripts are
far too large for argv), and the child env has the API-key variables stripped so
the CLI authenticates with the logged-in Claude Code subscription rather than
burning API credits.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import msvcrt  # Windows: the bot's home
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl

DEFAULT_CLI_MODEL = "sonnet"
DEFAULT_TIMEOUT = 600  # seconds; a long transcript can take minutes

# Stripped from the CHILD env so the CLI bills the subscription, never the API key.
_API_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeCliError(Exception):
    """Base for every CLI failure."""


class QuotaExceeded(ClaudeCliError):
    """Subscription usage limit hit — retry after a long back-off."""


class AuthFailure(ClaudeCliError):
    """Not logged in / credentials rejected — needs a human, never retry."""


class CliTimeout(ClaudeCliError):
    """The CLI did not finish within the timeout."""


class CliError(ClaudeCliError):
    """Any other CLI failure."""


def build_command(model: str = DEFAULT_CLI_MODEL) -> list[str]:
    return ["claude", "-p", "--output-format", "json", "--model", model]


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    for var in _API_ENV_VARS:
        env.pop(var, None)
    return env


_QUOTA_MARKERS = ("usage limit", "rate limit", "quota", "too many requests",
                  "overloaded")
_AUTH_MARKERS = ("not logged in", "unauthorized", "authentication",
                 "invalid api key", "please run /login", "credit balance")


def classify_failure(returncode, envelope, stderr):
    """Map a finished run to a failure class, or None when it succeeded.

    `is_error` in the envelope is authoritative: the CLI can exit 0 on failure.
    """
    envelope = envelope or {}
    failed = bool(envelope.get("is_error")) or returncode != 0 or not envelope
    if not failed:
        return None

    status = envelope.get("api_error_status")
    haystack = " ".join(
        str(part) for part in (envelope.get("result"), envelope.get("subtype"), stderr)
        if part
    ).lower()

    if status in (401, 403) or any(m in haystack for m in _AUTH_MARKERS):
        return AuthFailure
    if status == 429 or any(m in haystack for m in _QUOTA_MARKERS):
        return QuotaExceeded
    return CliError


def invoke_once(prompt: str, *, cwd: Path, model: str = DEFAULT_CLI_MODEL,
                timeout: int = DEFAULT_TIMEOUT) -> str:
    """One CLI attempt. Returns the assistant text, or raises a typed error."""
    try:
        proc = subprocess.run(
            build_command(model),
            input=prompt,               # stdin, never argv
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(cwd),
            env=child_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CliTimeout(f"claude CLI timed out after {timeout}s") from exc
    except OSError as exc:  # CLI missing / not executable
        raise CliError(f"could not run the claude CLI: {exc}") from exc

    try:
        envelope = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        envelope = None

    failure = classify_failure(proc.returncode, envelope, proc.stderr or "")
    if failure is not None:
        detail = ""
        if envelope:
            detail = str(envelope.get("result") or envelope.get("subtype") or "")
        detail = detail or (proc.stderr or "").strip() or f"exit code {proc.returncode}"
        raise failure(detail[:500])

    # The SDK path used to reject notes cut off at the token limit; the CLI
    # reports the same condition via stop_reason, so keep that guard — a
    # silently truncated note must never reach the vault.
    if str(envelope.get("stop_reason") or "").lower() == "max_tokens":
        raise CliError("claude CLI response was truncated (stop_reason=max_tokens)")

    text = envelope.get("result") or ""
    if not text.strip():
        raise CliError("claude CLI returned an empty result")
    return text


QUOTA_BACKOFF_START = 600      # 10 minutes
QUOTA_BACKOFF_MAX = 3600       # cap each individual sleep at 1 hour
QUOTA_MAX_TOTAL_WAIT = 14400   # give up on a video after 4 hours of waiting
MAX_TRANSIENT_ATTEMPTS = 3     # timeouts / generic errors
DAILY_ATTEMPT_CAP = 200        # global across all videos, resets at midnight


class DailyCapReached(ClaudeCliError):
    """The daily CLI attempt budget is exhausted."""


def attempt_state_path() -> Path:
    from . import config
    return config.state_dir() / "cli-attempts.json"


@contextlib.contextmanager
def _state_lock(path: Path):
    """Exclusive lock around every read and write of the counter. The bot and
    parallel backfill runners share the file; unlocked, a reader could catch it
    mid-write (emptied, not yet rewritten) and restart the day at 0."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+b") as fh:
        if msvcrt:
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # retries ~10 s, then raises
                    break
                except OSError:
                    continue
        else:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if msvcrt:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _read_count(path: Path, today: str) -> int:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        # Under the lock a torn write can't be seen, so anything unexpected is a
        # genuinely corrupt file: counting is best-effort, start fresh.
        if isinstance(loaded, dict) and loaded.get("date") == today:
            return int(loaded.get("attempts", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return 0


def attempts_today(today: str | None = None) -> int:
    """Today's attempt count, without counting an attempt."""
    today = today or dt.date.today().isoformat()
    path = attempt_state_path()
    with _state_lock(path):
        return _read_count(path, today)


def record_attempt(today: str | None = None) -> int:
    """Increment and return today's attempt count (resets on a new date)."""
    today = today or dt.date.today().isoformat()
    path = attempt_state_path()
    attempts = 1
    try:
        with _state_lock(path):
            attempts = _read_count(path, today) + 1
            path.write_text(json.dumps({"date": today, "attempts": attempts}),
                            encoding="utf-8")
    except OSError:
        pass  # counting is best-effort; never block work on the state file
    return attempts


def run_claude(prompt: str, *, cwd: Path, model: str = DEFAULT_CLI_MODEL,
               timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run the CLI with the retry policy. Raises a typed ClaudeCliError.

    Quota refusals get a long back-off (the subscription window has to reset);
    everything else is either a short bounded retry or an immediate failure.
    """
    transient_attempts = 0
    quota_wait_total = 0
    quota_backoff = QUOTA_BACKOFF_START

    while True:
        used = record_attempt()
        if used > DAILY_ATTEMPT_CAP:
            raise DailyCapReached(
                f"daily CLI attempt cap reached ({DAILY_ATTEMPT_CAP}); "
                "resets at local midnight"
            )
        try:
            return invoke_once(prompt, cwd=cwd, model=model, timeout=timeout)
        except AuthFailure as exc:
            # A human has to re-login; retrying only burns the daily budget.
            print(f"claude CLI auth failure — run `claude` and /login: {exc}",
                  file=sys.stderr, flush=True)
            raise
        except QuotaExceeded:
            if quota_wait_total >= QUOTA_MAX_TOTAL_WAIT:
                raise
            sleep_for = min(quota_backoff, QUOTA_MAX_TOTAL_WAIT - quota_wait_total)
            print(f"claude CLI quota reached; waiting {sleep_for}s before retry",
                  file=sys.stderr, flush=True)
            time.sleep(sleep_for)
            quota_wait_total += sleep_for
            quota_backoff = min(quota_backoff * 2, QUOTA_BACKOFF_MAX)
        except (CliTimeout, CliError):
            transient_attempts += 1
            if transient_attempts >= MAX_TRANSIENT_ATTEMPTS:
                raise
            time.sleep(2 ** transient_attempts)
