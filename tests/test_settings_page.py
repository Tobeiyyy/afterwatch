import http.client
import json
import re
import threading

import pytest

from afterwatch import config, settings_page
from afterwatch.claude_cli import AuthFailure

SECRET = "not-a-real-key"


class Client:
    def __init__(self, server, url):
        self.port = server.server_address[1]
        self.token = url.split("?t=")[1]

    def call(self, method, path, body=None, *, token="ok", headers=None, host="default", raw=None,
             ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}" if host == "default" else host}
        if token == "ok":
            hdrs["X-Afterwatch-Token"] = self.token
        elif token:
            hdrs["X-Afterwatch-Token"] = token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            hdrs["Content-Type"] = ctype
        hdrs.update(headers or {})
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp, payload

    def json(self, method, path, body=None, **kw):
        resp, payload = self.call(method, path, body, **kw)
        return resp.status, json.loads(payload)

    def post(self, path, body, **kw):
        return self.json("POST", path, body, **kw)

    def state(self):
        return self.json("GET", "/api/config")[1]

    def page(self, **kw):
        return self.call("GET", f"/?t={self.token}", token=None, **kw)


@pytest.fixture
def store(monkeypatch):
    """A fake credential store; the real one is never touched."""
    data = {}
    keyring = pytest.importorskip("keyring")
    monkeypatch.setattr(keyring, "get_password", lambda svc, name: data.get((svc, name)))
    monkeypatch.setattr(keyring, "set_password", lambda svc, name, key: data.__setitem__((svc, name), key))

    def delete(svc, name):
        data.pop((svc, name), None)
    monkeypatch.setattr(keyring, "delete_password", delete)
    for p in config.PROVIDERS.values():
        if p.get("key_env"):
            monkeypatch.delenv(p["key_env"], raising=False)
    return data


@pytest.fixture
def c(store):
    server, url = settings_page.make_server()
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield Client(server, url)
    server.shutdown()
    server.server_close()
    thread.join(5)


def saved_file_exists():
    return config.config_path().exists()


# --- access control ---

def test_server_binds_loopback_only():
    server, url = settings_page.make_server()
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert url.startswith(f"http://127.0.0.1:{server.server_address[1]}/?t=")
    finally:
        server.server_close()


def test_page_needs_the_token(c):
    assert c.call("GET", "/", token=None)[0].status == 403
    assert c.call("GET", "/?t=wrong", token=None)[0].status == 403
    assert c.call("GET", "/?t=", token=None)[0].status == 403
    resp, body = c.page()
    assert resp.status == 200 and b"Afterwatch" in body


def test_token_with_non_ascii_is_rejected_not_a_crash(c):
    conn = http.client.HTTPConnection("127.0.0.1", c.port, timeout=10)
    conn.request("GET", "/api/config", headers={"X-Afterwatch-Token": "tök", "Host": f"127.0.0.1:{c.port}"})
    assert conn.getresponse().status == 403


def test_api_needs_the_token_header(c):
    assert c.json("GET", "/api/config", token=None)[0] == 403
    assert c.json("GET", "/api/config", token="wrong")[0] == 403
    assert c.json("GET", "/api/config")[0] == 200
    # the token in the query string does not unlock the API
    assert c.json("GET", f"/api/config?t={c.token}", token=None)[0] == 403
    assert c.post("/api/config", {"limits": {"daily_calls": 5}}, token=None)[0] == 403
    assert c.post("/api/key", {"provider": "openai", "key": "x"}, token="wrong")[0] == 403
    assert not saved_file_exists()


def test_unknown_paths_are_404(c):
    assert c.json("GET", "/nope")[0] == 404
    assert c.json("POST", "/api/nope", {})[0] == 404


def test_wrong_host_is_rejected(c):
    assert c.page(host="evil.example:80")[0].status == 403
    assert c.page(host="127.0.0.1")[0].status == 403          # port missing
    assert c.json("GET", "/api/config", host="evil.example")[0] == 403
    assert c.page(host=f"localhost:{c.port}")[0].status == 200


def test_bad_origin_on_post_is_rejected(c):
    body = {"limits": {"daily_calls": 7}}
    assert c.post("/api/config", body, headers={"Origin": "http://evil.example"})[0] == 403
    assert c.post("/api/config", body, headers={"Origin": "null"})[0] == 403
    assert c.post("/api/config", body, headers={"Origin": f"http://127.0.0.1:{c.port + 1}"})[0] == 403
    assert not saved_file_exists()
    assert c.post("/api/config", body, headers={"Origin": f"http://127.0.0.1:{c.port}"})[0] == 200
    assert c.post("/api/config", body)[0] == 200              # no Origin header at all


def test_body_limits(c):
    big = json.dumps({"notes": {"extra_instructions": "x" * (settings_page.MAX_BODY + 1)}}).encode()
    assert c.call("POST", "/api/config", raw=big)[0].status == 413
    assert c.call("POST", "/api/config", raw=b"{not json")[0].status == 400
    assert c.call("POST", "/api/config", raw=b"[1]")[0].status == 400
    assert c.call("POST", "/api/config", raw=b"{}", ctype="text/plain")[0].status == 415
    assert c.json("POST", "/api/config", {})[0] == 200        # empty object is a valid no-op


def test_security_headers_on_page_and_api(c):
    for resp, _ in (c.page(), c.call("GET", "/api/config")):
        csp = resp.getheader("Content-Security-Policy")
        assert "default-src 'none'" in csp and "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.getheader("Referrer-Policy") == "no-referrer"
    resp, body = c.page()
    nonce = re.search(r"script-src 'nonce-([^']+)'", resp.getheader("Content-Security-Policy")).group(1)
    assert f'<script nonce="{nonce}">'.encode() in body and f'<style nonce="{nonce}">'.encode() in body


def test_page_loads_nothing_from_the_network(c):
    html = c.page()[1].decode()
    assert not re.search(r"<link|<img|<iframe|<script[^>]*\ssrc=|@import|url\(", html)
    assert c.token in html                                    # the page needs it for its API calls
    assert "__TOKEN__" not in html and "__NONCE__" not in html


# --- keys are write-only ---

def test_key_is_never_echoed(c, store):
    status, body = c.post("/api/key", {"provider": "openai", "key": SECRET})
    assert status == 200 and body == {"source": "stored"}
    assert store[("afterwatch", "openai")] == SECRET
    assert SECRET.encode() not in c.page()[1]
    resp, raw = c.call("GET", "/api/config")
    assert SECRET.encode() not in raw
    assert json.loads(raw)["keys"]["openai"] == "stored"
    assert json.loads(raw)["keys"]["gemini"] == ""
    assert SECRET not in json.dumps(c.post("/api/config", {})[1])


def test_youtube_key_is_write_only_and_removable(c, store):
    assert c.state()["keys"]["youtube"] == ""
    assert c.post("/api/key", {"provider": "youtube", "key": SECRET})[1] == {"source": "stored"}
    assert store[("afterwatch", "youtube")] == SECRET
    assert SECRET.encode() not in c.page()[1] and SECRET not in json.dumps(c.state())
    assert c.state()["keys"]["youtube"] == "stored"
    assert c.post("/api/key", {"provider": "youtube", "key": ""})[1] == {"source": ""}
    assert store == {}
    # the name is reserved, so a custom provider cannot shadow it
    status, resp = c.post("/api/config", {"providers": {"youtube": {"base_url": "https://x.test"}}})
    assert status == 400 and "providers.youtube" in resp["errors"]


def test_key_from_environment_is_reported_as_env(c, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    state = c.state()
    assert state["keys"]["gemini"] == "env"
    assert SECRET not in json.dumps(state)


def test_empty_key_removes_it(c, store):
    c.post("/api/key", {"provider": "notion", "key": SECRET})
    assert c.state()["keys"]["notion"] == "stored"
    status, body = c.post("/api/key", {"provider": "notion", "key": ""})
    assert status == 200 and body == {"source": ""}
    assert store == {}


def test_key_rejects_unknown_provider_and_bad_values(c, store):
    assert c.post("/api/key", {"provider": "nope", "key": "x"})[0] == 400
    assert c.post("/api/key", {"provider": "claude-code", "key": "x"})[0] == 400   # signed-in CLI, no key
    assert c.post("/api/key", {"key": "x"})[0] == 400
    assert c.post("/api/key", {"provider": "openai", "key": 5})[0] == 400
    assert c.post("/api/key", {"provider": "openai", "key": "k" * 4097})[0] == 400
    assert c.post("/api/key", {"provider": "discord", "key": "k" * 4096})[0] == 200   # boundary
    assert ("afterwatch", "openai") not in store


def test_keyring_failure_does_not_echo_the_key(c, monkeypatch):
    import keyring

    def boom(*a):
        raise RuntimeError(f"cannot store {a[-1]}")
    monkeypatch.setattr(keyring, "set_password", boom)
    status, body = c.post("/api/key", {"provider": "openai", "key": SECRET})
    assert status == 500 and "RuntimeError" in body["error"] and SECRET not in json.dumps(body)


# --- validation ---

@pytest.mark.parametrize("body, path", [
    ({"notes": {"length": "huge"}}, "notes.length"),                     # bad enum
    ({"backfill": {"strictness": "harsh"}}, "backfill.strictness"),
    ({"screenshots": {"mode": True}}, "screenshots.mode"),
    ({"limits": {"daily_calls": 0}}, "limits.daily_calls"),              # out of range, low
    ({"limits": {"daily_calls": 100001}}, "limits.daily_calls"),         # out of range, high
    ({"limits": {"daily_calls": "5"}}, "limits.daily_calls"),            # wrong type
    ({"limits": {"daily_calls": True}}, "limits.daily_calls"),           # bool is not a number here
    ({"limits": {"daily_calls": 5.5}}, "limits.daily_calls"),
    ({"bot": {"hub_hour": 24}}, "bot.hub_hour"),
    ({"bot": {"hub_weekday": -1}}, "bot.hub_weekday"),
    ({"bot": {"channel": "abc"}}, "bot.channel"),
    ({"hubs": {"enabled": "yes"}}, "hubs.enabled"),
    ({"notes": {"bogus": 1}}, "notes.bogus"),                            # unknown key
    ({"bogus": {}}, "bogus"),                                            # unknown section
    ({"notes": "short"}, "notes"),
    ({"notes": {"language": ""}}, "notes.language"),
    ({"notes": {"disabled_schemas": ["Z"]}}, "notes.disabled_schemas"),
    ({"notes": {"disabled_schemas": ["AB"]}}, "notes.disabled_schemas"),
    ({"notes": {"disabled_schemas": list("ABCDEF")}}, "notes.disabled_schemas"),
    ({"backfill": {"drop_channels": "one"}}, "backfill.drop_channels"),
    ({"backfill": {"drop_channels": [1]}}, "backfill.drop_channels"),
    ({"folders": {"notes": "../out"}}, "folders.notes"),
    ({"folders": {"hubs": "/abs"}}, "folders.hubs"),
    ({"folders": {"tags": "C:\\x"}}, "folders.tags"),
    ({"folders": {"images": ""}}, "folders.images"),
    ({"hubs": {"min_notes": 20, "max_notes": 10}}, "hubs.max_notes"),    # cross-field
    ({"models": {"notes": "nope:model"}}, "models.notes"),               # unknown provider
    ({"models": {"notes": "gpt-5"}}, "models.notes"),                    # no provider
    ({"models": {"notes": "openai:"}}, "models.notes"),                  # api needs a model name
    ({"models": {"notes": "openai: gpt"}}, "models.notes"),
    ({"models": {"extra": "openai:gpt"}}, "models.extra"),               # unknown task
])
def test_invalid_input_is_rejected_per_field_and_nothing_is_saved(c, body, path):
    status, resp = c.post("/api/config", body)
    assert status == 400 and path in resp["errors"] and resp["errors"][path]
    assert not saved_file_exists()


def test_boundaries_are_accepted(c):
    body = {"limits": {"daily_calls": 1}, "bot": {"hub_hour": 23, "hub_weekday": 0, "channel": ""},
            "backfill": {"min_minutes": 0}, "hubs": {"min_notes": 5, "max_notes": 5},
            "models": {"notes": "claude-code:", "hubs": ""}}
    assert c.post("/api/config", body)[0] == 200
    assert c.post("/api/config", {"limits": {"daily_calls": 100000}})[0] == 200
    cfg = config.load()
    assert cfg["limits"]["daily_calls"] == 100000 and cfg["models"]["notes"] == "claude-code:"


def test_errors_are_reported_together(c):
    status, resp = c.post("/api/config", {"notes": {"length": "x"}, "limits": {"daily_calls": 0}})
    assert status == 400 and set(resp["errors"]) == {"notes.length", "limits.daily_calls"}


def test_save_round_trip_through_config_load(c):
    body = {
        "models": {"notes": "gemini:some-model", "vision": "openai:other-model"},
        "limits": {"daily_calls": 50},
        "notes": {"length": "thorough", "style": "study", "extra_instructions": "Line one.\r\nLine two.",
                  "language": "English", "disabled_schemas": ["F", "E", "F"]},
        "screenshots": {"mode": "always", "max_per_note": 6},
        "output": {"target": "markdown", "path": "some/folder", "notion_parent": ""},
        "folders": {"notes": "Notes/Videos", "failures": "Failed.md"},
        "hubs": {"enabled": False, "min_notes": 2, "max_notes": 9},
        "backfill": {"min_minutes": 12, "drop_channels": [" A ", "", "B"], "judge": False, "strictness": "lenient"},
        "bot": {"channel": "123456", "hub_weekday": 2, "hub_hour": 20},
    }
    status, state = c.post("/api/config", body)
    assert status == 200
    cfg = config.load()
    assert cfg["models"]["notes"] == "gemini:some-model" and cfg["models"]["hubs"] == ""
    assert cfg["notes"]["disabled_schemas"] == ["E", "F"]
    assert cfg["notes"]["extra_instructions"] == "Line one.\nLine two."
    assert cfg["backfill"]["drop_channels"] == ["A", "B"]
    assert cfg["bot"] == {"channel": "123456", "hub_weekday": 2, "hub_hour": 20}
    assert cfg["folders"]["notes"] == "Notes/Videos" and cfg["folders"]["tags"] == "Tags"
    assert state["config"]["screenshots"] == {"mode": "always", "max_per_note": 6}
    assert c.state()["config"]["hubs"]["enabled"] is False


def test_partial_save_keeps_everything_else(c):
    c.post("/api/config", {"notes": {"style": "concise"}})
    c.post("/api/config", {"limits": {"daily_calls": 9}})
    cfg = config.load()
    assert cfg["notes"]["style"] == "concise" and cfg["limits"]["daily_calls"] == 9


def test_state_lists_providers_without_secrets(c):
    state = c.state()
    names = {p["name"]: p for p in state["providers"]}
    assert set(config.PROVIDERS) <= set(names) and names["openai"]["builtin"] is True
    assert names["claude-code"]["backend"] == "claude-code" and "claude-code" not in state["keys"]
    assert {"notion", "discord", "youtube"} <= set(state["keys"])
    assert state["tasks"] == list(config.TASKS)


# --- providers ---

def test_custom_provider_add_and_remove(c):
    add = {"providers": {"groq": {"backend": "api", "base_url": "https://example.test/v1",
                                  "key_env": "GROQ_API_KEY", "vision": False}},
           "models": {"notes": "groq:some-model"}}
    assert c.post("/api/config", add)[0] == 200
    cfg = config.load()
    assert cfg["providers"]["groq"] == {"backend": "api", "base_url": "https://example.test/v1",
                                        "key_env": "GROQ_API_KEY", "vision": False}
    assert "groq" in c.state()["keys"]
    # removing it while a task still points at it is refused, even if the request leaves models out
    status, resp = c.post("/api/config", {"providers": {}})
    assert status == 400 and "models.notes" in resp["errors"]
    status, resp = c.post("/api/config", {"providers": {}, "models": {"notes": "groq:some-model"}})
    assert status == 400 and "models.notes" in resp["errors"]
    assert c.post("/api/config", {"providers": {}, "models": {"notes": ""}})[0] == 200
    assert "groq" not in config.load()["providers"]
    assert "groq" not in {p["name"] for p in c.state()["providers"]}


@pytest.mark.parametrize("name, fields, path", [
    ("Bad Name", {"base_url": "https://x.test"}, "providers.Bad Name"),
    ("notion", {"base_url": "https://x.test"}, "providers.notion"),
    ("ok", {"base_url": "ftp://x.test"}, "providers.ok.base_url"),
    ("ok", {"base_url": "not a url"}, "providers.ok.base_url"),
    ("ok", {}, "providers.ok.base_url"),
    ("ok", {"base_url": "https://x.test", "key_env": "has space"}, "providers.ok.key_env"),
    ("ok", {"base_url": "https://x.test", "vision": "yes"}, "providers.ok.vision"),
    ("ok", {"base_url": "https://x.test", "backend": "claude-code"}, "providers.ok.backend"),
    ("ok", {"base_url": "https://x.test", "headers": {}}, "providers.ok.headers"),
])
def test_invalid_custom_provider(c, name, fields, path):
    status, resp = c.post("/api/config", {"providers": {name: fields}})
    assert status == 400 and path in resp["errors"]
    assert not saved_file_exists()


def test_preset_can_be_edited_but_not_removed(c):
    edit = {"providers": {"ollama": {"base_url": "http://localhost:9999/v1", "vision": True},
                          "claude-code": {"vision": False}}}
    assert c.post("/api/config", edit)[0] == 200
    cfg = config.load()
    assert cfg["providers"]["ollama"]["base_url"] == "http://localhost:9999/v1"
    assert cfg["providers"]["ollama"]["vision"] is True and cfg["providers"]["claude-code"]["vision"] is False
    assert c.post("/api/config", {"providers": {}})[0] == 200          # omitted presets stay
    assert set(config.PROVIDERS) <= set(config.load()["providers"])
    assert config.load()["providers"]["ollama"]["base_url"] == "http://localhost:9999/v1"
    # the signed-in CLI has no URL to edit
    status, resp = c.post("/api/config", {"providers": {"claude-code": {"base_url": "https://x.test"}}})
    assert status == 400 and "providers.claude-code.base_url" in resp["errors"]


def test_hand_edited_provider_extras_survive_a_save(c):
    cfg = config.load()
    cfg["providers"]["mine"] = {"backend": "api", "base_url": "https://x.test/v1", "key_env": "",
                                "vision": False, "headers": {"X-Org": "abc"}}
    config.save(cfg)
    body = {"providers": {"mine": {"backend": "api", "base_url": "https://y.test/v1", "key_env": "", "vision": True}}}
    assert c.post("/api/config", body)[0] == 200
    mine = config.load()["providers"]["mine"]
    assert mine["headers"] == {"X-Org": "abc"} and mine["base_url"] == "https://y.test/v1"
    assert "headers" not in json.dumps(c.state())


# --- test button ---

def test_test_endpoint_ok(c, monkeypatch):
    seen = {}

    def fake(task, prompt, *, cfg=None, **kw):
        seen.update(task=task, prompt=prompt, spec=cfg["models"]["notes"])
        return "  OK\n"
    monkeypatch.setattr(settings_page.llm, "complete", fake)
    c.post("/api/config", {"models": {"notes": "gemini:keep-me"}})
    status, body = c.post("/api/test", {"provider": "openai", "model": "some-model"})
    assert status == 200 and body == {"ok": True, "reply": "OK"}
    assert seen == {"task": "notes", "prompt": "Reply with the single word OK.", "spec": "openai:some-model"}
    assert config.load()["models"]["notes"] == "gemini:keep-me"      # the saved config is untouched


def test_test_endpoint_reports_errors_without_the_key(c, monkeypatch, store):
    c.post("/api/key", {"provider": "openai", "key": SECRET})

    def fail(*a, **kw):
        raise AuthFailure(f"openai rejected the API key (401): bad key {SECRET}")
    monkeypatch.setattr(settings_page.llm, "complete", fail)
    status, body = c.post("/api/test", {"provider": "openai", "model": "m"})
    assert status == 200 and body["ok"] is False and body["error"] == "AuthFailure"
    assert "rejected" in body["message"] and SECRET not in json.dumps(body)


def test_test_endpoint_rejects_bad_input(c, monkeypatch):
    monkeypatch.setattr(settings_page.llm, "complete", lambda *a, **k: pytest.fail("must not be called"))
    assert c.post("/api/test", {"provider": "nope", "model": "m"})[0] == 400
    assert c.post("/api/test", {"model": "m"})[0] == 400
    assert c.post("/api/test", {"provider": "openai", "model": "two words"})[0] == 400
    assert c.post("/api/test", {"provider": "openai", "model": 3})[0] == 400


def test_serve_prints_the_url_and_stops_on_ctrl_c(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(settings_page.webbrowser, "open", opened.append)
    monkeypatch.setattr(settings_page._Server, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    settings_page.serve(open_browser=True)
    out = capsys.readouterr().out
    assert opened and opened[0] in out and "/?t=" in out


# --- theme ---

def test_page_is_dark_by_default(c):
    resp, page = c.page()
    assert resp.status == 200 and b'data-theme="dark"' in page


def test_theme_toggle_is_saved_and_served(c):
    assert c.post("/api/theme", {"theme": "light"}) == (200, {"theme": "light"})
    assert config.load()["ui"]["theme"] == "light"
    assert b'data-theme="light"' in c.page()[1]
    assert c.post("/api/theme", {"theme": "dark"})[0] == 200
    assert b'data-theme="dark"' in c.page()[1]


def test_theme_rejects_bad_values_and_needs_the_token(c):
    assert c.post("/api/theme", {"theme": "neon"})[0] == 400
    assert c.post("/api/theme", {"theme": "light"}, token=None)[0] == 403
    assert config.load()["ui"]["theme"] == "dark"
