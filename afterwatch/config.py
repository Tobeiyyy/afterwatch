"""One config file for everything, plus API keys kept out of it.

The config is TOML in the user's config directory (override the location with
AFTERWATCH_CONFIG). Anything missing falls back to DEFAULTS, so an empty file
is a valid config. API keys are never written here: they come from an
environment variable or the OS credential store (``keyring``), see api_key().
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tomllib
from pathlib import Path

CONFIG_ENV = "AFTERWATCH_CONFIG"
KEYRING_SERVICE = "afterwatch"

# Base URLs checked against each provider's own docs on 2026-10-28. "vision" is
# whether its models accept images; for the ones where that depends on the model
# or the docs don't say, it starts off and the user can switch it on.
PROVIDERS = {
    "openai":     {"backend": "api", "base_url": "https://api.openai.com/v1",
                   "key_env": "OPENAI_API_KEY", "vision": True},
    "gemini":     {"backend": "api", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                   "key_env": "GEMINI_API_KEY", "vision": True},
    "anthropic":  {"backend": "api", "base_url": "https://api.anthropic.com/v1/",
                   "key_env": "ANTHROPIC_API_KEY", "vision": True, "max_tokens": 16000},
    "xai":        {"backend": "api", "base_url": "https://api.x.ai/v1",
                   "key_env": "XAI_API_KEY", "vision": False},
    "deepseek":   {"backend": "api", "base_url": "https://api.deepseek.com",
                   "key_env": "DEEPSEEK_API_KEY", "vision": False},
    "openrouter": {"backend": "api", "base_url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "vision": False},
    "ollama":     {"backend": "api", "base_url": "http://localhost:11434/v1",
                   "key_env": "", "vision": False},
    # The user's own installed and signed-in Claude Code CLI. Afterwatch never
    # sees that sign-in; it only runs `claude -p`.
    "claude-code": {"backend": "claude-code", "vision": True},
}

# Tasks a model can be chosen for. Value: "provider:model".
TASKS = ("notes", "hubs", "hub_planning", "vision", "judge")

DEFAULTS = {
    "output": {
        "target": "obsidian",       # obsidian | markdown | notion | docx | txt
        "path": "",                 # vault or folder; for notion: unused
        "notion_parent": "",        # Notion page id notes are created under
    },
    "folders": {
        "notes": "Videos",
        "tags": "Tags",
        "hubs": "Hubs",
        "images": "Images",
        "failures": "Afterwatch - Failed videos.md",
    },
    "providers": {},                # user overrides/additions, merged over PROVIDERS
    "models": {t: "" for t in TASKS},
    "limits": {
        "daily_calls": 200,         # all tasks together; resets at local midnight
    },
    "notes": {
        "length": "normal",         # short | normal | thorough
        "style": "standard",        # standard | concise | study | detailed
        "extra_instructions": "",
        "language": "auto",         # "auto" = the video's language, else e.g. "English"
        "disabled_schemas": [],     # any of A B C D E F
    },
    "screenshots": {
        "mode": "auto",             # off | auto | always
        "max_per_note": 3,
    },
    "hubs": {
        "enabled": True,
        "min_notes": 5,
        "max_notes": 15,
        "rewrite_after": 3,
        "max_per_run": 10,
        "update_every_days": 7,
    },
    "backfill": {
        "min_minutes": 5,
        "drop_channels": [],
        "judge": True,
        "strictness": "aggressive",  # aggressive | balanced | lenient
    },
    "ui": {
        "theme": "dark",            # settings page: dark | light
    },
    "bot": {
        "channel": "",
        "hub_weekday": 6,           # Monday = 0
        "hub_hour": 8,
    },
}


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "afterwatch"


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV) or config_dir() / "config.toml")


def state_dir() -> Path:
    """Counters, the hub manifest and other runtime state, next to the config."""
    return config_path().parent / "state"


def folder(name: str, cfg: dict | None = None) -> str:
    """A vault-relative folder (or, for "failures", file) from [folders]."""
    return (cfg or load())["folders"][name]


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    user = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = _merge(DEFAULTS, user)
    cfg["providers"] = _merge(PROVIDERS, user.get("providers", {}))
    return cfg


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # TOML basic strings share JSON's escapes


def dumps(cfg: dict) -> str:
    """Serialise the plain tables Afterwatch uses (strings, numbers, bools,
    lists, nested tables). Not a general TOML writer."""
    lines: list[str] = []

    def table(prefix: str, d: dict):
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        if scalars or not prefix:
            if prefix:
                lines.append(f"\n[{prefix}]")
            for k, v in scalars.items():
                lines.append(f"{_key(k)} = {_toml_value(v)}")
        for k, v in d.items():
            if isinstance(v, dict):
                table(f"{prefix}.{_key(k)}" if prefix else _key(k), v)

    table("", cfg)
    return "\n".join(lines).lstrip("\n") + "\n"


def _key(k: str) -> str:
    return k if k.replace("-", "").replace("_", "").isalnum() else json.dumps(k)


def save(cfg: dict, path: Path | None = None) -> Path:
    """Write only what differs from the defaults, so upgrades can change defaults."""
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    diff = _diff(DEFAULTS | {"providers": PROVIDERS}, cfg)
    path.write_text(dumps(diff), encoding="utf-8")
    return path


def _diff(base: dict, cfg: dict) -> dict:
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            sub = _diff(base[k], v)
            if sub:
                out[k] = sub
        elif base.get(k, object()) != v:
            out[k] = v
    return out


# --- API keys: environment first, then the OS credential store ---

def api_key(provider: str, cfg: dict | None = None) -> str | None:
    cfg = cfg or load()
    env = cfg["providers"].get(provider, {}).get("key_env") or ""
    if env and os.environ.get(env):
        return os.environ[env]
    if os.environ.get(f"AFTERWATCH_{provider.upper().replace('-', '_')}_KEY"):
        return os.environ[f"AFTERWATCH_{provider.upper().replace('-', '_')}_KEY"]
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, provider)
    except Exception:  # noqa: BLE001 - no usable keyring backend: env vars only
        return None


def set_api_key(provider: str, key: str) -> None:
    import keyring
    if key:
        keyring.set_password(KEYRING_SERVICE, provider, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, provider)
        except Exception:  # noqa: BLE001 - nothing stored
            pass


def model_for(task: str, cfg: dict | None = None) -> tuple[str, str]:
    """(provider, model) for a task. Raises with a pointer to the settings page
    when nothing is configured."""
    cfg = cfg or load()
    spec = cfg["models"].get(task) or cfg["models"].get("notes") or ""
    provider, _, model = spec.partition(":")
    if not provider or provider not in cfg["providers"]:
        raise ConfigError(f"no model set for '{task}'. Run `afterwatch settings` and pick one "
                          f"(or set models.{task} = \"provider:model\" in {config_path()})")
    return provider, model


class ConfigError(Exception):
    pass
