"""The local settings page behind `afterwatch settings`.

A tiny web server on 127.0.0.1 that edits the config file and stores API keys.
Only this machine can reach it, every request needs a per-session token, and
keys are write-only: the page and the API only ever say whether one is set.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import config, llm

MAX_BODY = 64 * 1024
SCHEMA_LETTERS = "ABCDEF"
EXTRA_KEYS = ("notion", "discord", "youtube")   # write-only secrets that belong to no model provider
SECTIONS = ("output", "folders", "limits", "notes", "screenshots", "hubs", "backfill", "bot")


# --- validation: each validator returns the cleaned value or raises ValueError(message) ---

def _enum(*options):
    def check(v):
        if v not in options:
            raise ValueError("choose one of: " + ", ".join(options))
        return v
    return check


def _int(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"enter a whole number from {lo} to {hi}")
        return v
    return check


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("must be on or off")
    return v


def _text(limit, *, required=False, multiline=False, pattern=None, what=""):
    def check(v):
        if not isinstance(v, str):
            raise ValueError("must be text")
        v = v.replace("\r\n", "\n") if multiline else v.strip()
        if len(v) > limit:
            raise ValueError(f"at most {limit} characters")
        if required and not v.strip():
            raise ValueError("cannot be empty")
        if re.search(r"[\x00-\x08\x0b-\x1f]" if multiline else r"[\x00-\x1f]", v):
            raise ValueError("contains control characters")
        if pattern and v and not re.fullmatch(pattern, v):
            raise ValueError(what)
        return v
    return check


def _folder(v):
    v = _text(200, required=True)(v)
    if re.search(r'[<>:"|?*]', v) or v.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", v):
        raise ValueError("use a plain relative folder name (no drive letter, no leading slash, no ..)")
    return v


def _lines(v):
    if not isinstance(v, list) or len(v) > 500 or not all(isinstance(x, str) for x in v):
        raise ValueError("must be a list of names (at most 500)")
    out = [x.strip() for x in v if x.strip()]
    if any(len(x) > 200 or re.search(r"[\x00-\x1f]", x) for x in out):
        raise ValueError("each name must be one line, at most 200 characters")
    return out


def _schemas(v):
    if (not isinstance(v, list) or not all(isinstance(x, str) for x in v)
            or any(x not in tuple(SCHEMA_LETTERS) for x in v)):
        raise ValueError("must be a list of letters from " + ", ".join(SCHEMA_LETTERS))
    if len(set(v)) == len(SCHEMA_LETTERS):
        raise ValueError("leave at least one note layout enabled")
    return sorted(set(v))


_SPEC = {
    "output": {
        "target": _enum("obsidian", "markdown", "notion", "docx", "txt"),
        "path": _text(500),
        "notion_parent": _text(200),
    },
    "folders": {k: _folder for k in config.DEFAULTS["folders"]},
    "limits": {"daily_calls": _int(1, 100000)},
    "notes": {
        "length": _enum("short", "normal", "thorough"),
        "style": _enum("standard", "concise", "study", "detailed"),
        "extra_instructions": _text(4000, multiline=True),
        "language": _text(40, required=True),
        "disabled_schemas": _schemas,
    },
    "screenshots": {"mode": _enum("off", "auto", "always"), "max_per_note": _int(1, 12)},
    "hubs": {
        "enabled": _bool,
        "min_notes": _int(1, 100),
        "max_notes": _int(1, 200),
        "rewrite_after": _int(1, 100),
        "max_per_run": _int(1, 100),
        "update_every_days": _int(1, 365),
    },
    "backfill": {
        "min_minutes": _int(0, 600),
        "drop_channels": _lines,
        "judge": _bool,
        "strictness": _enum("aggressive", "balanced", "lenient"),
    },
    "ui": {"theme": _enum("dark", "light")},
    "bot": {
        "channel": _text(25, pattern=r"\d+", what="a Discord channel id is digits only"),
        "hub_weekday": _int(0, 6),
        "hub_hour": _int(0, 23),
    },
}

_PROVIDER_FIELDS = {
    "base_url": None,   # filled below
    "key_env": _text(64, pattern=r"[A-Za-z_][A-Za-z0-9_]*", what="letters, digits and underscores"),
    "vision": _bool,
}


def _base_url(v):
    v = _text(300)(v)
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.netloc or re.search(r"\s", v):
        raise ValueError("must be a full http:// or https:// address")
    return v


_PROVIDER_FIELDS["base_url"] = _base_url
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def _providers(raw, current, errors):
    """The provider table after the edit: every preset (editable), plus exactly
    the custom providers present in ``raw``. Hand-edited extras such as
    headers are kept."""
    if not isinstance(raw, dict):
        errors["providers"] = "expected an object"
        return current
    out = {n: copy.deepcopy(current[n]) for n in config.PROVIDERS}
    for name, fields in raw.items():
        path = f"providers.{name}"
        preset = name in config.PROVIDERS
        if not preset and name not in current and (
                not _NAME.fullmatch(name) or name in EXTRA_KEYS):
            errors[path] = "use 1-32 lowercase letters, digits, - or _, and not a reserved name"
            continue
        if not isinstance(fields, dict):
            errors[path] = "expected an object"
            continue
        entry = out[name] if preset else copy.deepcopy(current.get(name, {"backend": "api"}))
        allowed = set(_PROVIDER_FIELDS) if not preset or entry["backend"] == "api" else {"vision"}
        if not preset:
            allowed.add("backend")
        for k, v in fields.items():
            try:
                if k not in allowed:
                    raise ValueError("cannot be changed")
                if k == "backend":
                    if v != "api":
                        raise ValueError("custom providers use the OpenAI-compatible API backend")
                else:
                    entry[k] = _PROVIDER_FIELDS[k](v)
            except ValueError as exc:
                errors[f"{path}.{k}"] = str(exc)
        if not preset and not entry.get("base_url"):
            errors.setdefault(f"{path}.base_url", "enter the provider's base URL")
        if not preset:
            entry["backend"] = "api"
            entry.setdefault("key_env", "")
            entry.setdefault("vision", False)
        out[name] = entry
    return out


def _models(raw, providers, errors):
    if not isinstance(raw, dict):
        errors["models"] = "expected an object"
        return None
    out = {}
    for task, spec in raw.items():
        path = f"models.{task}"
        if task not in config.TASKS:
            errors[path] = "unknown task"
        elif not isinstance(spec, str) or re.search(r"\s", spec) or len(spec) > 200:
            errors[path] = "use provider:model, without spaces"
        elif spec:
            name, sep, model = spec.partition(":")
            if not sep or name not in providers:
                errors[path] = "use provider:model with a provider from the table: " + ", ".join(providers)
            elif not model and providers[name]["backend"] != "claude-code":
                errors[path] = "enter a model name"
            else:
                out[task] = spec
        else:
            out[task] = ""
    return out


def apply_settings(payload) -> tuple[dict, dict]:
    """Validate ``payload`` against the current config. Returns (new config,
    {field path: message}); the config is only meaningful when there are no errors."""
    cfg = config.load()
    errors: dict[str, str] = {}
    if not isinstance(payload, dict):
        return cfg, {"": "expected a JSON object"}
    for sec in payload:
        if sec not in _SPEC and sec not in ("models", "providers"):
            errors[sec] = "unknown section"
    if "providers" in payload:
        cfg["providers"] = _providers(payload["providers"], cfg["providers"], errors)
    if "models" in payload:
        cfg["models"].update(_models(payload["models"], cfg["providers"], errors) or {})
    if "providers" in payload:   # a removed provider must not stay selected for a task
        for task, spec in cfg["models"].items():
            if spec and spec.partition(":")[0] not in cfg["providers"]:
                errors.setdefault(f"models.{task}", "this provider was removed; pick another")
    for sec, keys in _SPEC.items():
        if sec not in payload:
            continue
        if not isinstance(payload[sec], dict):
            errors[sec] = "expected an object"
            continue
        for key, value in payload[sec].items():
            try:
                if key not in keys:
                    raise ValueError("unknown setting")
                cfg[sec][key] = keys[key](value)
            except ValueError as exc:
                errors[f"{sec}.{key}"] = str(exc)
    h = cfg["hubs"]
    if "hubs.min_notes" not in errors and "hubs.max_notes" not in errors and h["min_notes"] > h["max_notes"]:
        errors["hubs.max_notes"] = "must be at least the minimum number of notes"
    return cfg, errors


# --- state shown to the page: never contains a key ---

def _key_source(name: str, cfg: dict) -> str:
    env = cfg["providers"].get(name, {}).get("key_env") or ""
    if (env and os.environ.get(env)) or os.environ.get(f"AFTERWATCH_{name.upper().replace('-', '_')}_KEY"):
        return "env"
    return "stored" if config.api_key(name, cfg) else ""


def _key_names(cfg: dict) -> list[str]:
    return [n for n, p in cfg["providers"].items() if p.get("backend") != "claude-code"] + list(EXTRA_KEYS)


def _state(cfg: dict) -> dict:
    return {
        "config": {s: cfg[s] for s in (*SECTIONS, "models")},
        "providers": [
            {"name": n, "backend": p.get("backend", "api"), "base_url": p.get("base_url", ""),
             "key_env": p.get("key_env", ""), "vision": bool(p.get("vision")),
             "builtin": n in config.PROVIDERS}
            for n, p in cfg["providers"].items()],
        "keys": {n: _key_source(n, cfg) for n in _key_names(cfg)},
        "tasks": list(config.TASKS),
    }


# --- API handlers: (server, body) -> (status, json) ---

def _get_config(server, _body):
    return 200, _state(config.load())


def _post_config(server, body):
    cfg, errors = apply_settings(body)
    if errors:
        return 400, {"errors": errors}
    with server.lock:
        config.save(cfg)
    return 200, _state(config.load())


def _post_key(server, body):
    cfg = config.load()
    name, key = body.get("provider"), body.get("key", "")
    if not isinstance(name, str) or name not in _key_names(cfg):
        return 400, {"error": "unknown provider (save new providers before adding a key)"}
    if not isinstance(key, str) or len(key) > 4096:
        return 400, {"error": "the key must be text of at most 4096 characters"}
    try:
        with server.lock:
            config.set_api_key(name, key.strip())
    except Exception as exc:  # noqa: BLE001 - never echo anything that could carry the key
        return 500, {"error": f"could not use the credential store ({type(exc).__name__})"}
    return 200, {"source": _key_source(name, cfg)}


def _post_test(server, body):
    cfg = config.load()
    name, model = body.get("provider"), body.get("model", "")
    if not isinstance(name, str) or name not in cfg["providers"]:
        return 400, {"error": "unknown provider (save new providers first)"}
    if not isinstance(model, str) or re.search(r"\s", model) or len(model) > 200:
        return 400, {"error": "enter a model name without spaces"}
    cfg["models"]["notes"] = f"{name}:{model}"
    try:
        reply = llm.complete("notes", "Reply with the single word OK.", cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - the page shows whatever went wrong
        message = str(exc)
        key = config.api_key(name, cfg)
        if key:
            message = message.replace(key, "***")
        return 200, {"ok": False, "error": type(exc).__name__, "message": message[:500]}
    return 200, {"ok": True, "reply": reply.strip()[:200]}


def _post_theme(server, body):
    """Dark or light page, saved right away (the port changes every run, so the
    browser can't remember it)."""
    try:
        theme = _SPEC["ui"]["theme"](body.get("theme"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    with server.lock:
        cfg = config.load()
        cfg["ui"]["theme"] = theme
        config.save(cfg)
    return 200, {"theme": theme}


def _theme() -> str:
    try:
        return config.load()["ui"]["theme"]
    except Exception:  # noqa: BLE001 - a broken config is reported by the page itself
        return "dark"


_ROUTES = {("GET", "/api/config"): _get_config, ("POST", "/api/config"): _post_config,
           ("POST", "/api/theme"): _post_theme,
           ("POST", "/api/key"): _post_key, ("POST", "/api/test"): _post_test}


# --- HTTP plumbing ---

class _Handler(BaseHTTPRequestHandler):
    server_version = "Afterwatch"
    sys_version = ""

    def log_message(self, *args):
        """Silent: request lines carry the session token."""

    def _send(self, status, body: bytes, ctype: str, nonce: str | None = None):
        script = f"script-src 'nonce-{nonce}'" if nonce else "script-src 'none'"
        style = f"style-src 'self' 'nonce-{nonce}'" if nonce else "style-src 'none'"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; {style}; {script}; connect-src 'self'; "
                         "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _allowed(self) -> bool:
        """Host must be our own (DNS rebinding), and a POST's Origin, if sent, our own page."""
        hosts = self.server.hosts
        if self.headers.get("Host") not in hosts:
            self._json(403, {"error": "forbidden host"})
            return False
        origin = self.headers.get("Origin")
        if self.command == "POST" and origin is not None and origin not in {f"http://{h}" for h in hosts}:
            self._json(403, {"error": "forbidden origin"})
            return False
        return True

    def _token_ok(self, candidate) -> bool:
        return isinstance(candidate, str) and secrets.compare_digest(
            candidate.encode("utf-8"), self.server.token.encode("utf-8"))

    def _route(self, path):
        """The handler for this request, or None after sending the 404/403."""
        handler = _ROUTES.get((self.command, path))
        if handler is None:
            self._json(404, {"error": "not found"})
        elif not self._token_ok(self.headers.get("X-Afterwatch-Token")):
            self._json(403, {"error": "missing or wrong token"})
            return None
        return handler

    def _run(self, handler, body):
        try:
            status, obj = handler(self.server, body)
        except Exception as exc:  # noqa: BLE001 - a broken config file must not kill the server
            status, obj = 500, {"error": f"{type(exc).__name__}: {exc}"}
        self._json(status, obj)

    def do_GET(self):
        if not self._allowed():
            return
        url = urlsplit(self.path)
        if url.path != "/":
            handler = self._route(url.path)
            return self._run(handler, {}) if handler else None
        if not self._token_ok((parse_qs(url.query).get("t") or [None])[0]):
            return self._json(403, {"error": "open the link printed by `afterwatch settings`"})
        nonce = secrets.token_urlsafe(16)
        page = (PAGE.replace("__NONCE__", nonce).replace("__TOKEN__", self.server.token)
                .replace("__THEME__", "light" if _theme() == "light" else "dark"))
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", nonce)

    def do_POST(self):
        if not self._allowed():
            return
        handler = self._route(urlsplit(self.path).path)
        if handler is None:
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._json(411, {"error": "Content-Length required"})
        if length < 0 or length > MAX_BODY:
            return self._json(413, {"error": f"body too large (limit {MAX_BODY // 1024} KB)"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(415, {"error": "send application/json"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"errors": {"": "expected a JSON object"}})
        self._run(handler, body)


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False   # on Windows this would let another process share the port

    def __init__(self, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.hosts = {f"127.0.0.1:{self.server_address[1]}", f"localhost:{self.server_address[1]}"}


def make_server(port: int = 0):
    """(server, url). Call ``server.serve_forever()``; port 0 picks a free one."""
    server = _Server(port)
    return server, f"http://127.0.0.1:{server.server_address[1]}/?t={server.token}"


def serve(open_browser: bool = True, port: int = 0) -> None:
    server, url = make_server(port)
    print(f"Afterwatch settings: {url}\nPress Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


PAGE = r"""<!doctype html>
<html lang="en" data-theme="__THEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>Afterwatch Settings</title>
<style nonce="__NONCE__">
:root {
  color-scheme: dark;
  --bg: #0f1216; --surface: #171b21; --sunken: #1e232a; --border: #2a303a; --text: #e7eaef; --muted: #9aa4b2;
  --accent: #8aa3ff; --accent-ink: #0d1530; --accent-soft: #1d2745;
  --ok: #55c68a; --ok-soft: #14291f; --bad: #ff8573; --bad-soft: #301a17; --warn: #e8c05a; --warn-soft: #2b2411;
  --shadow: 0 1px 2px rgba(0, 0, 0, .3);
  --font: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #f5f6f8; --surface: #ffffff; --sunken: #f0f2f5; --border: #e1e4ea; --text: #1b2028; --muted: #5c6673;
  --accent: #3556d8; --accent-ink: #ffffff; --accent-soft: #e9eefc;
  --ok: #1a7f4b; --ok-soft: #e4f4ea; --bad: #b8321f; --bad-soft: #fbeae7; --warn: #8a5a00; --warn-soft: #fff4d6;
  --shadow: 0 1px 2px rgba(20, 28, 40, .05), 0 4px 16px rgba(20, 28, 40, .04);
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { scroll-behavior: smooth; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 var(--font); -webkit-font-smoothing: antialiased; }
h1, h2, p { margin: 0; }

.top { position: sticky; top: 0; z-index: 10; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: blur(10px); border-bottom: 1px solid var(--border); }
.top-in { max-width: 1060px; margin: 0 auto; padding: 0 20px; height: 60px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.brand { display: flex; align-items: center; gap: 10px; }
.logo { width: 22px; height: 22px; border-radius: 7px; background: var(--accent); position: relative; flex: none; }
.logo::after { content: ""; position: absolute; left: 8px; top: 6px; border-left: 8px solid var(--accent-ink); border-top: 5px solid transparent; border-bottom: 5px solid transparent; }
h1 { font-size: 1.05rem; font-weight: 650; letter-spacing: -.01em; }
h1 span { color: var(--muted); font-weight: 500; }
.save { display: flex; align-items: center; gap: 14px; }
.status { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: .88rem; }
.status::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--border); }
.status.dirty { color: var(--warn); } .status.dirty::before { background: var(--warn); }
.status.saved { color: var(--ok); } .status.saved::before { background: var(--ok); }
.status.bad { color: var(--bad); } .status.bad::before { background: var(--bad); }

.shell { max-width: 1060px; margin: 0 auto; padding: 28px 20px 96px; display: grid; grid-template-columns: 188px minmax(0, 1fr); gap: 36px; }
nav { position: sticky; top: 88px; align-self: start; display: flex; flex-direction: column; gap: 2px; }
nav a { color: var(--muted); text-decoration: none; padding: 7px 12px; border-radius: 8px; font-size: .92rem; border-left: 2px solid transparent; }
nav a:hover { color: var(--text); background: var(--sunken); }
nav a.on { color: var(--accent); background: var(--accent-soft); font-weight: 600; }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

main { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
.lede { color: var(--muted); max-width: 62ch; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px 26px 10px; box-shadow: var(--shadow); scroll-margin-top: 80px; }
h2 { font-size: 1.12rem; font-weight: 650; letter-spacing: -.01em; }
.intro { color: var(--muted); margin-top: 4px; max-width: 64ch; }
h3 { font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 650; margin: 22px 0 0; }

.row { display: grid; grid-template-columns: minmax(0, 250px) minmax(0, 1fr); gap: 6px 28px; padding: 16px 0; border-top: 1px solid var(--border); margin-top: 0; }
.row:first-of-type { margin-top: 14px; }
.lab label, .lab .name { font-weight: 600; display: block; }
.hint { color: var(--muted); font-size: .86rem; margin-top: 2px; }
.ctl { min-width: 0; }
.err { color: var(--bad); font-size: .86rem; margin-top: 6px; }

input[type=text], input[type=password], input[type=number], select, textarea {
  font: inherit; color: var(--text); background: var(--surface); border: 1px solid var(--border); border-radius: 9px;
  padding: 8px 11px; width: 100%; max-width: 440px; }
input[type=number] { width: 120px; }
textarea { max-width: 560px; resize: vertical; line-height: 1.45; }
input:hover, select:hover, textarea:hover { border-color: color-mix(in srgb, var(--border) 55%, var(--muted)); }
input::placeholder, textarea::placeholder { color: color-mix(in srgb, var(--muted) 75%, transparent); }
select { appearance: auto; }
input:disabled { background: var(--sunken); color: var(--muted); }
.invalid input, .invalid select, .invalid textarea { border-color: var(--bad); }

.btn { font: inherit; font-size: .9rem; font-weight: 600; padding: 8px 15px; border-radius: 9px; border: 1px solid var(--border); background: var(--surface); color: var(--text); cursor: pointer; white-space: nowrap; }
.btn:hover:not(:disabled) { background: var(--sunken); }
.btn:disabled { opacity: .5; cursor: default; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.btn.primary:hover:not(:disabled) { background: var(--accent); filter: brightness(1.08); }
.btn.small { padding: 6px 11px; font-size: .84rem; }
.btn.danger { color: var(--bad); }

.seg { display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 3px; background: var(--sunken); border: 1px solid var(--border); border-radius: 11px; }
.seg label { position: relative; }
.seg input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; }
.seg span { display: block; padding: 5px 14px; border-radius: 8px; color: var(--muted); font-size: .9rem; cursor: pointer; }
.seg input:checked + span { background: var(--surface); color: var(--text); font-weight: 600; box-shadow: 0 1px 2px rgba(0, 0, 0, .12), 0 0 0 1px var(--border); }
.seg input:focus-visible + span { outline: 2px solid var(--accent); outline-offset: 1px; }

input.switch { appearance: none; -webkit-appearance: none; width: 40px; height: 23px; padding: 0; border-radius: 99px; background: var(--border); border: 0; position: relative; cursor: pointer; transition: background .15s; }
input.switch::after { content: ""; position: absolute; top: 2px; left: 2px; width: 19px; height: 19px; border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0, 0, 0, .3); transition: transform .15s; }
input.switch:checked { background: var(--accent); }
input.switch:checked::after { transform: translateX(17px); }

.checks { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 8px 16px; }
.checks label { display: flex; align-items: center; gap: 9px; cursor: pointer; }
.checks input { width: 17px; height: 17px; accent-color: var(--accent); margin: 0; }

.model { display: grid; grid-template-columns: 170px minmax(0, 1fr); gap: 8px; max-width: 560px; }
.model select, .model input { max-width: none; }

.note { border-radius: 10px; padding: 11px 14px; font-size: .9rem; background: var(--accent-soft); margin-top: 14px; }
.note.warn { background: var(--warn-soft); color: var(--warn); }
.note.bad { background: var(--bad-soft); color: var(--bad); }
.note p + p { margin-top: 4px; }

.prov { border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; margin-top: 14px; background: var(--surface); }
.prov-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.prov-head strong { font-size: 1rem; }
.prov-head .grow { flex: 1; }
.tag, .badge { font-size: .74rem; font-weight: 600; padding: 2px 9px; border-radius: 99px; background: var(--sunken); color: var(--muted); }
.badge.ok { background: var(--ok-soft); color: var(--ok); }
.badge.off { background: var(--sunken); color: var(--muted); }
.pgrid { display: grid; grid-template-columns: 130px minmax(0, 1fr); gap: 12px 16px; margin-top: 14px; align-items: start; }
.pgrid > .pl { padding-top: 8px; font-size: .88rem; color: var(--muted); font-weight: 600; }
.key, .test { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.key input, .test input { flex: 1 1 200px; max-width: 280px; }
.result { font-size: .88rem; margin-top: 6px; overflow-wrap: anywhere; }
.result.good { color: var(--ok); } .result.bad { color: var(--bad); }
.msg { color: var(--muted); font-size: .86rem; width: 100%; }

details.add { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 14px; }
details.add summary { cursor: pointer; font-weight: 600; color: var(--accent); width: fit-content; border-radius: 6px; }
details.add .pgrid { margin-bottom: 14px; }
.card > :last-child { margin-bottom: 14px; }

@media (max-width: 820px) {
  .shell { grid-template-columns: minmax(0, 1fr); gap: 16px; padding-top: 16px; }
  nav { position: sticky; top: 60px; z-index: 5; flex-direction: row; overflow-x: auto; background: var(--bg); padding: 8px 0; margin: 0 -20px; padding-inline: 20px; border-bottom: 1px solid var(--border); scrollbar-width: none; }
  nav a { white-space: nowrap; }
  .card { scroll-margin-top: 130px; }
}
@media (max-width: 640px) {
  .row, .pgrid { grid-template-columns: minmax(0, 1fr); }
  .pgrid > .pl { padding-top: 0; }
  .card { padding: 18px 16px 6px; }
  .status { display: none; }
  .model { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } input.switch, input.switch::after { transition: none; } }
</style>
</head>
<body>
<header class="top"><div class="top-in">
  <div class="brand"><span class="logo" aria-hidden="true"></span><h1>Afterwatch <span>settings</span></h1></div>
  <div class="save"><span id="status" class="status" role="status" aria-live="polite"></span>
    <button id="theme" class="btn" type="button"></button>
    <button id="save" class="btn primary" type="button">Save changes</button></div>
</div></header>
<div class="shell">
  <nav id="nav" aria-label="Sections"></nav>
  <main id="content">
    <p class="lede">This page runs on your computer only. Settings are written to your Afterwatch config file when you press Save. API keys go to your system's credential store and are never shown again.</p>
    <div id="banner" class="note bad" role="alert" hidden></div>
    <noscript><p class="note bad">This page needs JavaScript.</p></noscript>
  </main>
</div>
<script nonce="__NONCE__">
(() => {
'use strict';
const TOKEN = '__TOKEN__';

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== false && v !== null && v !== undefined) e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c !== null && c !== undefined && c !== false) e.append(c);
  return e;
}
async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
    headers: {'X-Afterwatch-Token': TOKEN, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  let data = {};
  try { data = await r.json(); } catch (_) { /* not JSON */ }
  return {ok: r.ok, status: r.status, data};
}

let cfg, meta, savedJson, conds = [], justSaved = false;
const get = p => p.split('.').reduce((o, k) => o[k], cfg);
const set = (p, v) => { const ks = p.split('.'), last = ks.pop(); ks.reduce((o, k) => o[k], cfg)[last] = v; justSaved = false; update(); };
const clone = x => JSON.parse(JSON.stringify(x));

function adopt(d) {
  cfg = clone(d.config);
  cfg.providers = {};
  for (const p of d.providers) cfg.providers[p.name] = p;
  meta = {keys: d.keys};
  savedJson = JSON.stringify(cfg);
}

// ---- field rows ----
function row(id, label, hint, control, opts = {}) {
  const r = h('div', {class: 'row'},
    h('div', {class: 'lab'}, opts.group ? h('span', {class: 'name', id: 'l-' + id}, label) : h('label', {for: id, id: 'l-' + id}, label), h('p', {class: 'hint'}, hint)),
    h('div', {class: 'ctl'}, control, h('p', {class: 'err', 'data-err': opts.path || id, hidden: true})));
  if (opts.showIf) conds.push([r, opts.showIf]);
  return r;
}
const fid = path => 'f-' + path.replace(/\./g, '-');
function field(path, label, hint, make, opts = {}) {
  const id = fid(path);
  return row(id, label, hint, make(id, path), {...opts, path});
}
const C = {
  text: (o = {}) => (id, p) => h('input', {id, type: 'text', value: get(p), placeholder: o.ph, maxlength: o.max, autocomplete: 'off', spellcheck: 'false',
    oninput: e => set(p, e.target.value)}),
  num: (min, max) => (id, p) => h('input', {id, type: 'number', min, max, step: 1, value: get(p),
    oninput: e => set(p, e.target.value === '' ? null : Number(e.target.value))}),
  area: (rows, ph) => (id, p) => h('textarea', {id, rows, placeholder: ph, maxlength: 4000, oninput: e => set(p, e.target.value)}, get(p)),
  lines: (rows, ph) => (id, p) => h('textarea', {id, rows, placeholder: ph, spellcheck: 'false',
    oninput: e => set(p, e.target.value.split('\n').map(s => s.trim()).filter(Boolean))}, get(p).join('\n')),
  sel: (opts, asNum) => (id, p) => h('select', {id, onchange: e => set(p, asNum ? Number(e.target.value) : e.target.value)},
    opts.map(([v, t]) => h('option', {value: v, selected: v === get(p)}, t))),
  seg: opts => (id, p) => h('div', {id, class: 'seg', role: 'radiogroup', 'aria-labelledby': 'l-' + id},
    opts.map(([v, t]) => h('label', {}, h('input', {type: 'radio', name: id, value: v, checked: get(p) === v, onchange: () => set(p, v)}), h('span', {}, t)))),
  toggle: () => (id, p) => h('input', {id, type: 'checkbox', class: 'switch', role: 'switch', checked: get(p), onchange: e => set(p, e.target.checked)}),
};
const SCHEMAS = [['A', 'A: Technical guide'], ['B', 'B: Conceptual map'], ['C', 'C: Debate grid'], ['D', 'D: Narrative'], ['E', 'E: Recipe'], ['F', 'F: Media analysis (spoiler-labelled)']];
const notion = c => c.output.target === 'notion';

function modelRow(task, label, hint) {
  const path = 'models.' + task, id = fid(path), s = get(path), i = s.indexOf(':');
  let prov = i < 0 ? '' : s.slice(0, i), model = i < 0 ? '' : s.slice(i + 1);
  const names = Object.keys(cfg.providers);
  const apply = () => set(path, prov ? prov + ':' + model : '');
  const input = h('input', {type: 'text', value: model, 'aria-label': label + ' model name', autocomplete: 'off', spellcheck: 'false',
    placeholder: prov === 'claude-code' ? 'optional, default: sonnet' : 'model name', disabled: !prov,
    oninput: e => { model = e.target.value.trim(); apply(); }});
  const select = h('select', {id, onchange: e => {
      prov = e.target.value; input.disabled = !prov;
      input.placeholder = prov === 'claude-code' ? 'optional, default: sonnet' : 'model name'; apply(); }},
    h('option', {value: ''}, task === 'notes' ? 'Not set' : 'Same as notes'),
    prov && !names.includes(prov) ? h('option', {value: prov, selected: true}, prov + ' (unknown)') : null,
    names.map(n => h('option', {value: n, selected: n === prov}, n)));
  return row(id, label, hint, h('div', {class: 'model'}, select, input), {path});
}

// ---- keys (write-only) ----
function keyWidget(name) {
  const known = name in meta.keys;
  const badge = h('span', {class: 'badge'});
  const input = h('input', {type: 'password', autocomplete: 'new-password', spellcheck: 'false', placeholder: 'Paste a new key', 'aria-label': 'API key for ' + name, disabled: !known});
  const msg = h('p', {class: 'msg'}, known ? '' : 'Save the settings first, then add the key.');
  const add = h('button', {class: 'btn small', type: 'button', disabled: true}, 'Save key');
  const del = h('button', {class: 'btn small danger', type: 'button'}, 'Remove');
  const refresh = () => {
    const s = meta.keys[name] || '';
    badge.textContent = s === 'env' ? 'Set (environment variable)' : s ? 'Set' : 'Not set';
    badge.className = 'badge ' + (s ? 'ok' : 'off');
    del.hidden = s !== 'stored';
  };
  const send = async (key, done) => {
    add.disabled = del.disabled = true;
    const r = await api('/api/key', {provider: name, key});
    if (r.ok) { meta.keys[name] = r.data.source; input.value = ''; msg.textContent = done; }
    else msg.textContent = r.data.error || 'Could not save the key.';
    del.disabled = false; add.disabled = true; refresh();
  };
  input.addEventListener('input', () => { add.disabled = !input.value.trim(); });
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && input.value.trim()) add.click(); });
  add.addEventListener('click', () => send(input.value, 'Saved to your credential store.'));
  del.addEventListener('click', () => send('', 'Removed from your credential store.'));
  refresh();
  return h('div', {class: 'key'}, badge, input, add, del, msg);
}
function keyRow(name, label, hint, opts) {
  return row('key-' + name, label, hint, keyWidget(name), {...opts, group: true});
}

// ---- providers ----
function guessModel(name) {
  for (const v of Object.values(cfg.models)) if (v.startsWith(name + ':')) return v.slice(name.length + 1);
  return '';
}
function providerCard(name) {
  const p = cfg.providers[name], cli = p.backend === 'claude-code', ep = 'providers.' + name + '.';
  const grid = [];
  const line = (label, control, errKey) => grid.push(h('div', {class: 'pl'}, label), h('div', {}, control, errKey ? h('p', {class: 'err', 'data-err': ep + errKey, hidden: true}) : null));
  if (cli) {
    line('Sign-in', h('p', {class: 'hint'}, 'Uses your own installed and signed-in Claude Code CLI. Afterwatch never sees the sign-in, so there is no key and no URL to set.'));
  } else {
    line('Base URL', h('input', {type: 'text', value: p.base_url, 'aria-label': name + ' base URL', autocomplete: 'off', spellcheck: 'false',
      oninput: e => { p.base_url = e.target.value.trim(); justSaved = false; update(); }}), 'base_url');
    line('API key', keyWidget(name));
    if (!p.builtin) line('Key variable', h('input', {type: 'text', value: p.key_env, placeholder: 'optional, e.g. MY_API_KEY', 'aria-label': name + ' key environment variable', autocomplete: 'off', spellcheck: 'false',
      oninput: e => { p.key_env = e.target.value.trim(); justSaved = false; update(); }}), 'key_env');
    else if (p.key_env) line('Key variable', h('p', {class: 'hint'}, 'Also read from the environment variable ' + p.key_env + ' when set.'));
  }
  line('Reads images', h('input', {type: 'checkbox', class: 'switch', role: 'switch', checked: p.vision, 'aria-label': name + ' can read images',
    onchange: e => { p.vision = e.target.checked; justSaved = false; update(); }}), 'vision');
  const tm = h('input', {type: 'text', value: guessModel(name), placeholder: cli ? 'model (optional)' : 'model to test', 'aria-label': 'Model to test on ' + name, autocomplete: 'off', spellcheck: 'false'});
  const out = h('p', {class: 'result'});
  const btn = h('button', {class: 'btn small', type: 'button', onclick: async () => {
    btn.disabled = true; out.className = 'result'; out.textContent = 'Testing...';
    const r = await api('/api/test', {provider: name, model: tm.value.trim()});
    btn.disabled = false;
    if (r.ok && r.data.ok) { out.className = 'result good'; out.textContent = 'Works. The model replied: ' + r.data.reply; }
    else { out.className = 'result bad'; out.textContent = r.data.ok === false ? r.data.error + ': ' + r.data.message : (r.data.error || 'Request failed'); }
  }}, 'Test');
  line('Test', h('div', {}, h('div', {class: 'test'}, tm, btn), out));
  return h('div', {class: 'prov'},
    h('div', {class: 'prov-head'}, h('strong', {}, name), h('span', {class: 'tag'}, p.builtin ? 'Preset' : 'Custom'), h('span', {class: 'grow'}),
      p.builtin ? null : h('button', {class: 'btn small danger', type: 'button', onclick: () => { delete cfg.providers[name]; justSaved = false; render(); }}, 'Remove')),
    h('div', {class: 'pgrid'}, grid));
}
function addProvider() {
  const f = {name: h('input', {type: 'text', placeholder: 'e.g. groq', autocomplete: 'off', 'aria-label': 'New provider name'}),
    url: h('input', {type: 'text', placeholder: 'https://.../v1', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'New provider base URL'}),
    env: h('input', {type: 'text', placeholder: 'optional, e.g. MY_API_KEY', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'New provider key variable'}),
    vision: h('input', {type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': 'New provider can read images'})};
  const err = h('p', {class: 'err', hidden: true});
  const go = () => {
    const name = f.name.value.trim().toLowerCase();
    err.hidden = false;
    if (!/^[a-z0-9][a-z0-9_-]{0,31}$/.test(name) || name === 'notion' || name === 'discord') err.textContent = 'Name: 1-32 lowercase letters, digits, - or _.';
    else if (name in cfg.providers) err.textContent = 'That name is already in the table.';
    else if (!/^https?:\/\/\S+$/.test(f.url.value.trim())) err.textContent = 'Base URL must start with http:// or https://.';
    else {
      cfg.providers[name] = {name, backend: 'api', base_url: f.url.value.trim(), key_env: f.env.value.trim(), vision: f.vision.checked, builtin: false};
      justSaved = false; render(); return;
    }
  };
  const l = (t, c) => [h('div', {class: 'pl'}, t), h('div', {}, c)];
  return h('details', {class: 'add'}, h('summary', {}, 'Add an OpenAI-compatible provider'),
    h('div', {class: 'pgrid'}, l('Name', f.name), l('Base URL', f.url), l('Key variable', f.env), l('Reads images', f.vision),
      h('div', {}), h('div', {}, h('button', {class: 'btn small', type: 'button', onclick: go}, 'Add provider'), err)));
}

// ---- sections ----
const SECTIONS = [
  {id: 'models', title: 'Models', intro: 'Choose which model does each job. Only Notes is required: an empty choice below means the same as Notes.', build: () => [
    modelRow('notes', 'Notes', 'Writes the note for each video.'),
    modelRow('hubs', 'Hubs', 'Writes the topic hub pages.'),
    modelRow('hub_planning', 'Hub planning', 'Decides how notes are grouped into hubs.'),
    modelRow('vision', 'Screenshot vision', 'Looks at contact sheets. Needs a model that reads images.'),
    modelRow('judge', 'Backfill judge', 'Decides whether an old video is worth a note.'),
    h('h3', {}, 'Providers'),
    h('div', {class: 'note'},
      h('p', {}, h('strong', {}, 'Claude Code'), ' uses your own installed and signed-in Claude Code CLI. Afterwatch never sees the sign-in.'),
      h('p', {}, 'Gemini, OpenAI and the other providers work with an API key. Keys are stored in your system credential store, or read from the environment variable named on the provider.'),
      h('p', {}, 'Each Test is one real model call and counts against the daily cap. It uses the saved settings, so save first.')),
    Object.keys(cfg.providers).map(providerCard),
    addProvider()]},
  {id: 'limits', title: 'Limits', build: () => [
    field('limits.daily_calls', 'Daily call cap', 'The most model calls per day, all jobs together. Resets at local midnight. Retries count too.', C.num(1, 100000))]},
  {id: 'notes', title: 'Notes', intro: 'How each note is written.', build: () => [
    field('notes.length', 'Length', 'How much room a note gets.', C.seg([['short', 'Short'], ['normal', 'Normal'], ['thorough', 'Thorough']])),
    field('notes.style', 'Style', 'Concise keeps only what you would act on. Study defines terms and ends with review questions. Detailed keeps examples and reasoning.',
      C.seg([['standard', 'Standard'], ['concise', 'Concise'], ['study', 'Study'], ['detailed', 'Detailed']])),
    field('notes.extra_instructions', 'Extra instructions', 'Anything else the model should always do. Added to every note.', C.area(4, 'For example: always put a one-line summary first.')),
    field('notes.language', 'Language', 'Auto writes in the language of the video. Or type a language name, such as English.', C.text({ph: 'auto', max: 40})),
    field('notes.disabled_schemas', 'Note layouts', 'The model picks one layout per video. Untick any it must not use.', (id, p) =>
      h('div', {id, class: 'checks', role: 'group', 'aria-labelledby': 'l-' + id}, SCHEMAS.map(([k, t]) => h('label', {},
        h('input', {type: 'checkbox', checked: !get(p).includes(k), onchange: e => {
          const off = new Set(get(p)); e.target.checked ? off.delete(k) : off.add(k); set(p, [...off].sort()); }}), t))), {group: true})]},
  {id: 'screenshots', title: 'Screenshots', intro: 'Pictures from the video that are worth keeping in the note.', build: () => [
    field('screenshots.mode', 'Mode', 'Auto: a model looks at a contact sheet and keeps only real slides, charts, code and diagrams. Always skips the "is this visual?" check, rarely helps and costs more calls.',
      C.seg([['off', 'Off'], ['auto', 'Auto'], ['always', 'Always']])),
    field('screenshots.max_per_note', 'Most per note', 'The most screenshots one note can contain.', C.num(1, 12), {showIf: c => c.screenshots.mode !== 'off'}),
    h('div', {class: 'note warn', id: 'visionwarn', hidden: true})]},
  {id: 'output', title: 'Output', intro: 'Where finished notes end up.', build: () => [
    field('output.target', 'Target', 'Obsidian is recommended.', C.sel([['obsidian', 'Obsidian (recommended)'], ['markdown', 'Markdown folder'], ['notion', 'Notion'], ['docx', 'Word (.docx)'], ['txt', 'Plain text']])),
    field('output.path', 'Path', 'Your vault, or the folder notes are written to.', C.text({ph: 'Folder path', max: 500}), {showIf: c => !notion(c)}),
    field('output.notion_parent', 'Notion parent page', 'The id of the page new notes are created under.', C.text({max: 200}), {showIf: notion}),
    keyRow('notion', 'Notion token', 'Your Notion integration token. Write-only: it is never shown again.', {showIf: notion}),
    h('div', {class: 'note', id: 'hubnote'}, 'Hubs and the reading order need Obsidian or Markdown output.')]},
  {id: 'folders', title: 'Folders', intro: 'Names inside your vault or output folder.', build: () => [
    field('folders.notes', 'Notes', 'One note per video.', C.text({max: 200})),
    field('folders.tags', 'Tags', 'Tag pages.', C.text({max: 200})),
    field('folders.hubs', 'Hubs', 'Topic hub pages.', C.text({max: 200})),
    field('folders.images', 'Images', 'Screenshots saved with the notes.', C.text({max: 200})),
    field('folders.failures', 'Failures file', 'A note listing videos that could not be processed.', C.text({max: 200}))]},
  {id: 'hubs', title: 'Hubs', intro: 'Pages that pull related notes together by topic.', build: () => [
    field('hubs.enabled', 'Hubs', 'Write hub pages.', C.toggle()),
    field('hubs.min_notes', 'Fewest notes', 'A topic needs at least this many notes to get a hub.', C.num(1, 100), {showIf: c => c.hubs.enabled}),
    field('hubs.max_notes', 'Most notes', 'The most notes one hub covers.', C.num(1, 200), {showIf: c => c.hubs.enabled}),
    field('hubs.rewrite_after', 'Rewrite after', 'Rewrite a hub once this many new notes belong to it.', C.num(1, 100), {showIf: c => c.hubs.enabled}),
    field('hubs.max_per_run', 'Most per run', 'The most hubs written or updated in one run.', C.num(1, 100), {showIf: c => c.hubs.enabled}),
    field('hubs.update_every_days', 'Update every (days)', 'How often hubs are refreshed.', C.num(1, 365), {showIf: c => c.hubs.enabled})]},
  {id: 'backfill', title: 'Backfill', intro: 'Turning your watch history into notes.', build: () => [
    field('backfill.min_minutes', 'Shortest video (minutes)', 'Videos shorter than this are skipped.', C.num(0, 600)),
    field('backfill.drop_channels', 'Channels to skip', 'One channel name per line.', C.lines(4, 'One channel per line')),
    field('backfill.judge', 'Judge', 'A model decides whether each old video deserves a note. Uses model calls.', C.toggle()),
    field('backfill.strictness', 'Strictness', 'How firmly the judge filters. Aggressive drops the most, lenient keeps the most.',
      C.seg([['aggressive', 'Aggressive'], ['balanced', 'Balanced'], ['lenient', 'Lenient']]), {showIf: c => c.backfill.judge}),
    keyRow('youtube', 'YouTube Data API key (optional)', 'Speeds up `afterwatch backfill enrich`; without it, video details are read one by one more slowly. An API key only, never a YouTube login.')]},
  {id: 'bot', title: 'Bot (optional)', intro: 'Only needed if you run the Discord bot.', build: () => [
    field('bot.channel', 'Discord channel id', 'The channel the bot works in. Digits only.', C.text({ph: '123456789012345678', max: 25})),
    field('bot.hub_weekday', 'Weekly hub day', 'The day the weekly hub update runs.', C.sel(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((d, i) => [i, d]), true)),
    field('bot.hub_hour', 'Weekly hub hour', 'The hour of that day, 0 to 23.', C.sel([...Array(24).keys()].map(i => [i, String(i).padStart(2, '0') + ':00']), true)),
    keyRow('discord', 'Bot token', 'Your Discord bot token. Write-only: it is never shown again.')]},
];

// ---- page ----
const content = $ => document.querySelector($);
let io;
function setup() {
  const nav = content('#nav'), main = content('#content');
  for (const s of SECTIONS) {
    nav.append(h('a', {href: '#' + s.id, 'data-id': s.id}, s.title.replace(' (optional)', '')));
    main.append(h('section', {class: 'card', id: s.id, 'aria-labelledby': 'h-' + s.id}));
  }
  io = new IntersectionObserver(es => es.forEach(e => {
    if (e.isIntersecting) nav.querySelectorAll('a').forEach(a => a.classList.toggle('on', a.dataset.id === e.target.id));
  }), {rootMargin: '-15% 0px -75% 0px'});
  main.querySelectorAll('section').forEach(s => io.observe(s));
}
function render() {
  const y = window.scrollY;
  conds = [];
  for (const s of SECTIONS) {
    content('#' + s.id).replaceChildren(...[h('h2', {id: 'h-' + s.id}, s.title), s.intro && h('p', {class: 'intro'}, s.intro), s.build()].flat(Infinity).filter(Boolean));
  }
  update();
  window.scrollTo(0, y);
}
function update() {
  const dirty = JSON.stringify(cfg) !== savedJson;
  const st = content('#status');
  if (!st.classList.contains('bad') || dirty) {
    st.className = 'status' + (dirty ? ' dirty' : justSaved ? ' saved' : '');
    st.textContent = dirty ? 'Unsaved changes' : justSaved ? 'All changes saved' : 'No changes';
  }
  for (const [el, fn] of conds) el.hidden = !fn(cfg);
  const vp = (cfg.models.vision || cfg.models.notes).split(':')[0], w = content('#visionwarn');
  const bad = cfg.screenshots.mode !== 'off' && vp && cfg.providers[vp] && !cfg.providers[vp].vision;
  w.hidden = !bad;
  if (bad) w.textContent = 'The screenshot model uses ' + vp + ', which is set to not read images, so screenshots would fail. Turn on "Reads images" for ' + vp + ' under Models, choose another model, or set screenshots to Off.';
  const hn = content('#hubnote'), needs = cfg.hubs.enabled && !['obsidian', 'markdown'].includes(cfg.output.target);
  hn.className = 'note' + (needs ? ' warn' : '');
  hn.textContent = needs ? 'Hubs are on, but ' + cfg.output.target + ' output cannot hold hubs or the reading order. Use Obsidian or Markdown, or turn hubs off.'
    : 'Hubs and the reading order need Obsidian or Markdown output.';
}
function payload() {
  const out = {models: cfg.models, providers: {}};
  for (const s of ['output', 'folders', 'limits', 'notes', 'screenshots', 'hubs', 'backfill', 'bot']) out[s] = cfg[s];
  for (const [n, p] of Object.entries(cfg.providers)) {
    out.providers[n] = p.backend === 'claude-code' ? {vision: p.vision}
      : p.builtin ? {base_url: p.base_url, vision: p.vision}
      : {backend: 'api', base_url: p.base_url, key_env: p.key_env || '', vision: p.vision};
  }
  return out;
}
function clearErrors() {
  document.querySelectorAll('.err[data-err]').forEach(e => { e.hidden = true; e.textContent = ''; e.parentElement.classList.remove('invalid'); });
  content('#banner').hidden = true;
}
function showErrors(errors) {
  const other = [];
  let first = null;
  for (const [path, msg] of Object.entries(errors)) {
    const el = document.querySelector('.err[data-err="' + CSS.escape(path) + '"]');
    if (el && !el.parentElement.closest('[hidden]')) {
      el.textContent = msg; el.hidden = false; el.parentElement.classList.add('invalid'); first = first || el;
    } else other.push((path ? path + ': ' : '') + msg);
  }
  if (other.length) { const b = content('#banner'); b.textContent = other.join(' | '); b.hidden = false; first = first || b; }
  const st = content('#status');
  st.className = 'status bad'; st.textContent = Object.keys(errors).length + ' setting(s) need attention';
  if (first) first.scrollIntoView({block: 'center'});
}
async function save() {
  const btn = content('#save');
  btn.disabled = true; clearErrors(); content('#status').className = 'status';
  const r = await api('/api/config', payload());
  btn.disabled = false;
  if (r.ok) { adopt(r.data); justSaved = true; render(); }
  else if (r.status === 400 && r.data.errors) showErrors(r.data.errors);
  else { const st = content('#status'); st.className = 'status bad'; st.textContent = 'Not saved: ' + (r.data.error || 'HTTP ' + r.status); }
}

window.addEventListener('beforeunload', e => { if (cfg && JSON.stringify(cfg) !== savedJson) { e.preventDefault(); e.returnValue = ''; } });
content('#save').addEventListener('click', save);
function themeLabel() {
  content('#theme').textContent = document.documentElement.dataset.theme === 'light' ? 'Dark mode' : 'Light mode';
}
content('#theme').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  themeLabel();
  api('/api/theme', {theme: next});
});
themeLabel();
(async () => {
  const r = await api('/api/config');
  if (!r.ok) { content('#banner').textContent = 'Could not load the settings: ' + (r.data.error || 'HTTP ' + r.status); content('#banner').hidden = false; return; }
  adopt(r.data); setup(); render();
})();
})();
</script>
</body>
</html>
"""
