import io
import json
import urllib.error

import pytest

from afterwatch import claude_cli, config, llm

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"
JPG = b"\xff\xd8\xff" + b"rest"


def make_cfg(spec, **limits):
    cfg = config.load()
    cfg["models"]["notes"] = spec
    cfg["limits"].update(limits)
    return cfg


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(monkeypatch, replies):
    """replies: list of dicts (JSON answers) or ints (HTTP error codes), served in order."""
    sent = []

    def urlopen(req, timeout):
        sent.append({"url": req.full_url, "headers": dict(req.header_items()),
                     "body": json.loads(req.data)})
        r = replies.pop(0)
        if isinstance(r, int):
            raise urllib.error.HTTPError(req.full_url, r, "err", {}, io.BytesIO(b"detail"))
        return FakeResp(json.dumps(r).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return sent


def answer(text, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


@pytest.fixture
def openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


# --- API backend ---

def test_api_sends_system_and_prompt_with_bearer_key(monkeypatch, openai_key):
    sent = fake_urlopen(monkeypatch, [answer("note")])
    out = llm.complete("notes", "PROMPT", system="SYS", cfg=make_cfg("openai:gpt-5"))
    assert out == "note"
    req = sent[0]
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    assert req["body"]["model"] == "gpt-5"
    assert req["body"]["messages"] == [{"role": "system", "content": "SYS"},
                                       {"role": "user", "content": "PROMPT"}]


def test_api_model_override_and_images_as_data_urls(monkeypatch, openai_key):
    sent = fake_urlopen(monkeypatch, [answer("ok")])
    llm.complete("vision", "TASK", images=[("a:", PNG), ("b:", JPG)], model="gpt-5-mini",
                 cfg=make_cfg("openai:gpt-5"))
    body = sent[0]["body"]
    assert body["model"] == "gpt-5-mini"
    parts = body["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "text", "image_url", "text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[3]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert parts[-1]["text"] == "TASK"


def test_images_to_a_provider_without_vision_are_refused(monkeypatch):
    with pytest.raises(config.ConfigError, match="images"):
        llm.complete("vision", "T", images=[("a", PNG)], cfg=make_cfg("deepseek:deepseek-chat"))


def test_missing_api_key_is_an_auth_failure(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AFTERWATCH_GEMINI_KEY", raising=False)
    with pytest.raises(llm.AuthFailure, match="no API key"):
        llm.complete("notes", "P", cfg=make_cfg("gemini:gemini-3-flash"))


def test_rejected_key_is_an_auth_failure_without_retry(monkeypatch, openai_key):
    sent = fake_urlopen(monkeypatch, [401])
    with pytest.raises(llm.AuthFailure):
        llm.complete("notes", "P", cfg=make_cfg("openai:gpt-5"))
    assert len(sent) == 1


def test_rate_limit_is_retried_then_succeeds(monkeypatch, openai_key):
    sent = fake_urlopen(monkeypatch, [429, 503, answer("finally")])
    assert llm.complete("notes", "P", cfg=make_cfg("openai:gpt-5")) == "finally"
    assert len(sent) == 3


def test_rate_limit_every_time_becomes_quota_exceeded(monkeypatch, openai_key):
    fake_urlopen(monkeypatch, [429] * llm.MAX_API_RETRIES)
    with pytest.raises(llm.QuotaExceeded):
        llm.complete("notes", "P", cfg=make_cfg("openai:gpt-5"))


def test_truncated_and_empty_answers_are_errors(monkeypatch, openai_key):
    fake_urlopen(monkeypatch, [answer("half", finish="length"), answer("  ")])
    with pytest.raises(llm.CliError, match="truncated"):
        llm.complete("notes", "P", cfg=make_cfg("openai:gpt-5"))
    with pytest.raises(llm.CliError, match="empty"):
        llm.complete("notes", "P", cfg=make_cfg("openai:gpt-5"))


def test_every_api_try_counts_against_the_daily_cap(monkeypatch, openai_key):
    fake_urlopen(monkeypatch, [answer("1"), 429, answer("2"), answer("never")])
    cfg = make_cfg("openai:gpt-5", daily_calls=3)
    llm.complete("notes", "P", cfg=cfg)
    llm.complete("notes", "P", cfg=cfg)  # 429 + success = calls 2 and 3
    assert claude_cli.attempts_today() == 3
    with pytest.raises(llm.DailyCapReached):
        llm.complete("notes", "P", cfg=cfg)


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    sent = fake_urlopen(monkeypatch, [answer("local")])
    assert llm.complete("notes", "P", cfg=make_cfg("ollama:llama3")) == "local"
    assert sent[0]["url"] == "http://localhost:11434/v1/chat/completions"


# --- Claude Code backend ---

def _capture_cli(monkeypatch, reply="out"):
    seen = {}

    def fake_run_claude(prompt, *, cwd, model, **kw):
        seen.update(prompt=prompt, model=model,
                    files={p.name: p.read_bytes() for p in cwd.iterdir()})
        return reply

    monkeypatch.setattr(claude_cli, "run_claude", fake_run_claude)
    return seen


def test_claude_code_puts_system_prompt_in_claude_md(monkeypatch):
    seen = _capture_cli(monkeypatch)
    assert llm.complete("notes", "P", system="SYS", cfg=make_cfg("claude-code:opus")) == "out"
    assert seen["files"] == {"CLAUDE.md": b"SYS"}
    assert seen["model"] == "opus" and seen["prompt"] == "P"


def test_claude_code_without_model_uses_cli_default(monkeypatch):
    seen = _capture_cli(monkeypatch)
    llm.complete("notes", "P", cfg=make_cfg("claude-code:"))
    assert seen["model"] == claude_cli.DEFAULT_CLI_MODEL
    assert seen["files"] == {}


def test_claude_code_writes_images_and_names_them_in_order(monkeypatch):
    seen = _capture_cli(monkeypatch)
    llm.complete("vision", "TASK", images=[("first:", PNG), ("second:", JPG)],
                 cfg=make_cfg("claude-code:sonnet"))
    assert seen["files"] == {"image1.png": PNG, "image2.jpg": JPG}
    p = seen["prompt"]
    assert p.index("first:") < p.index("image1.png") < p.index("second:") < p.index("image2.jpg")
    assert p.rstrip().endswith("TASK")
