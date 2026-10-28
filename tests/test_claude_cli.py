import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from afterwatch import claude_cli
from afterwatch.claude_cli import (
    AuthFailure,
    CliError,
    CliTimeout,
    QuotaExceeded,
    build_command,
    child_env,
    classify_failure,
    invoke_once,
)

SUCCESS_ENVELOPE = {
    "type": "result", "subtype": "success", "is_error": False,
    "api_error_status": None, "result": "the note text", "stop_reason": "end_turn",
}


def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_build_command_shape():
    cmd = build_command("sonnet")
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--output-format" in cmd and "json" in cmd
    assert "--model" in cmd and "sonnet" in cmd


def test_child_env_strips_api_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("PATH_MARKER_FOR_TEST", "keepme")
    env = child_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH_MARKER_FOR_TEST"] == "keepme"


def test_classify_success_returns_none():
    assert classify_failure(0, SUCCESS_ENVELOPE, "") is None


def test_classify_quota():
    env = {"is_error": True, "result": "Usage limit reached. Try again later."}
    assert classify_failure(0, env, "") is QuotaExceeded
    assert classify_failure(1, None, "rate limit exceeded") is QuotaExceeded
    assert classify_failure(0, {"is_error": True, "api_error_status": 429}, "") is QuotaExceeded


def test_classify_auth():
    assert classify_failure(1, None, "Invalid API key / not logged in") is AuthFailure
    assert classify_failure(0, {"is_error": True, "api_error_status": 401}, "") is AuthFailure


def test_classify_generic_error_on_is_error_true_with_exit_zero():
    # exit code 0 must NOT be trusted on its own
    assert classify_failure(0, {"is_error": True, "result": "something broke"}, "") is CliError


def test_classify_generic_error_on_nonzero_exit():
    assert classify_failure(2, None, "boom") is CliError


def test_invoke_once_success_passes_prompt_on_stdin(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        seen["cwd"] = kwargs.get("cwd")
        seen["env"] = kwargs.get("env")
        return _completed(json.dumps(SUCCESS_ENVELOPE))

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    out = invoke_once("MY PROMPT BODY", cwd=tmp_path, model="sonnet")
    assert out == "the note text"
    assert seen["input"] == "MY PROMPT BODY"           # stdin, not argv
    assert not any("MY PROMPT BODY" in str(a) for a in seen["cmd"])
    assert str(seen["cwd"]) == str(tmp_path)
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_invoke_once_raises_on_is_error_true(tmp_path, monkeypatch):
    bad = {"is_error": True, "result": "Usage limit reached", "api_error_status": None}
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda cmd, **kw: _completed(json.dumps(bad)))
    with pytest.raises(QuotaExceeded):
        invoke_once("p", cwd=tmp_path)


def test_invoke_once_raises_on_timeout(tmp_path, monkeypatch):
    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    with pytest.raises(CliTimeout):
        invoke_once("p", cwd=tmp_path)


def test_invoke_once_raises_on_truncated_response(tmp_path, monkeypatch):
    """A note cut off at the token limit must never reach the vault."""
    truncated = dict(SUCCESS_ENVELOPE, stop_reason="max_tokens",
                     result="# Half a note that stops mid-sen")
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda cmd, **kw: _completed(json.dumps(truncated)))
    with pytest.raises(CliError):
        invoke_once("p", cwd=tmp_path)


def test_invoke_once_raises_on_unparseable_output(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli.subprocess, "run",
                        lambda cmd, **kw: _completed("not json at all"))
    with pytest.raises(CliError):
        invoke_once("p", cwd=tmp_path)
