"""Every model call goes through complete(task, ...).

The task ("notes", "hubs", "hub_planning", "vision", "judge") picks the
provider and model from the config. Two backends:

- api: any OpenAI-compatible chat-completions endpoint (OpenAI, Gemini,
  Anthropic, xAI, DeepSeek, OpenRouter, Ollama, ...), standard-library HTTP.
- claude-code: the user's own installed, signed-in Claude Code CLI. Afterwatch
  never handles that sign-in.

Both count against the same daily call cap (claude_cli's locked counter).
"""
from __future__ import annotations

import base64
import json
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import claude_cli, config
from .claude_cli import (AuthFailure, ClaudeCliError, CliError, CliTimeout, DailyCapReached,
                         QuotaExceeded)

API_TIMEOUT = 600
MAX_API_RETRIES = 5


def complete(task: str, prompt: str, *, system: str | None = None,
             images: list[tuple[str, bytes]] | None = None,
             model: str | None = None, cfg: dict | None = None) -> str:
    """The model's text answer. ``images``: (label, jpeg/png bytes) pairs.
    ``model`` overrides the configured model name for this one call."""
    cfg = cfg or config.load()
    provider, configured = config.model_for(task, cfg)
    p = cfg["providers"][provider]
    name = model or configured
    if images and not p.get("vision"):
        raise config.ConfigError(
            f"the '{task}' model ({provider}) is not set up to read images; pick a vision "
            "model for screenshots or turn screenshots off")
    claude_cli.DAILY_ATTEMPT_CAP = int(cfg["limits"]["daily_calls"])

    if p["backend"] == "claude-code":
        return _claude_code(prompt, system, images, name or claude_cli.DEFAULT_CLI_MODEL)
    if p["backend"] == "api":
        return _api(provider, p, name, prompt, system, images, cfg)
    raise config.ConfigError(f"unknown backend '{p['backend']}' for provider {provider}")


def _claude_code(prompt, system, images, model) -> str:
    """The CLI takes its system prompt from a CLAUDE.md in its working directory
    and opens image files there with its Read tool (no permission needed)."""
    with tempfile.TemporaryDirectory(prefix="afterwatch-") as tmp:
        work = Path(tmp)
        if system:
            (work / "CLAUDE.md").write_text(system, encoding="utf-8")
        head = ""
        if images:
            lines = []
            for n, (label, data) in enumerate(images, 1):
                path = work / f"image{n}.{_ext(data)}"
                path.write_bytes(data)
                lines.append(f"{label} {path}")
            head = ("Use the Read tool to open each image file below, in order, before "
                    "answering.\n" + "\n".join(lines) + "\n\n")
        return claude_cli.run_claude(head + prompt, cwd=work, model=model)


def _ext(data: bytes) -> str:
    return "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"


def _api(provider, p, model, prompt, system, images, cfg) -> str:
    if not model:
        raise config.ConfigError(f"no model name set for provider {provider}")
    key = config.api_key(provider, cfg) or ("ollama" if provider == "ollama" else None)
    if not key:
        raise AuthFailure(f"no API key for {provider}: run `afterwatch settings` or set "
                          f"{p.get('key_env') or 'its key'}")
    content: list[dict] | str = prompt
    if images:
        content = []
        for label, data in images:
            mime = "image/png" if _ext(data) == "png" else "image/jpeg"
            content.append({"type": "text", "text": label})
            content.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}})
        content.append({"type": "text", "text": prompt})
    messages = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": content}]
    body = {"model": model, "messages": messages}
    if p.get("max_tokens"):
        body["max_tokens"] = int(p["max_tokens"])

    url = p["base_url"].rstrip("/") + "/chat/completions"
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    headers.update(p.get("headers", {}))

    for attempt in range(1, MAX_API_RETRIES + 1):
        used = claude_cli.record_attempt()
        if used > claude_cli.DAILY_ATTEMPT_CAP:
            raise DailyCapReached(f"daily CLI attempt cap reached ({claude_cli.DAILY_ATTEMPT_CAP}); "
                                  "resets at local midnight")
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                reply = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            if exc.code in (401, 403):
                raise AuthFailure(f"{provider} rejected the API key ({exc.code}): {detail}") from exc
            if exc.code == 429 or exc.code >= 500:
                if attempt == MAX_API_RETRIES:
                    raise QuotaExceeded(f"{provider} usage limit or overload ({exc.code}): {detail}") from exc
                time.sleep(min(60, 2 ** attempt))
                continue
            raise CliError(f"{provider} returned {exc.code}: {detail}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == MAX_API_RETRIES:
                raise CliTimeout(f"{provider} did not answer: {exc}") from exc
            time.sleep(2 ** attempt)

    choice = (reply.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise CliError(f"{provider} response was truncated (finish_reason=length)")
    text = (choice.get("message") or {}).get("content") or ""
    if isinstance(text, list):  # some providers return content parts
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    if not text.strip():
        raise CliError(f"{provider} returned an empty answer")
    return text


__all__ = ["complete", "ClaudeCliError", "AuthFailure", "QuotaExceeded", "DailyCapReached",
           "CliError", "CliTimeout"]
