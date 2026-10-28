import tomllib

import pytest

from afterwatch import config


def test_empty_config_is_all_defaults():
    cfg = config.load()
    assert cfg["notes"]["length"] == "normal"
    assert cfg["limits"]["daily_calls"] == 200
    assert cfg["providers"]["gemini"]["base_url"].startswith("https://generativelanguage")


def test_state_dir_sits_next_to_the_config(tmp_path):
    assert config.state_dir() == config.config_path().parent / "state"


def test_save_writes_only_changes_and_round_trips():
    cfg = config.load()
    cfg["notes"]["style"] = "study"
    cfg["models"]["notes"] = "openai:gpt-5"
    cfg["backfill"]["drop_channels"] = ["Some \"Channel\""]
    path = config.save(cfg)
    written = tomllib.loads(path.read_text(encoding="utf-8"))
    assert written == {"models": {"notes": "openai:gpt-5"}, "notes": {"style": "study"},
                       "backfill": {"drop_channels": ["Some \"Channel\""]}}
    assert config.load() == cfg


def test_user_provider_overrides_merge_over_presets():
    config.config_path().parent.mkdir(parents=True)
    config.config_path().write_text(
        '[providers.ollama]\nvision = true\n[providers.mine]\nbackend = "api"\n'
        'base_url = "http://box:8000/v1"\n', encoding="utf-8")
    cfg = config.load()
    assert cfg["providers"]["ollama"]["vision"] is True
    assert cfg["providers"]["ollama"]["base_url"] == "http://localhost:11434/v1"
    assert cfg["providers"]["mine"]["base_url"] == "http://box:8000/v1"


def test_model_for_falls_back_to_the_notes_model():
    cfg = config.load()
    cfg["models"]["notes"] = "gemini:gemini-3-flash"
    assert config.model_for("judge", cfg) == ("gemini", "gemini-3-flash")
    cfg["models"]["judge"] = "deepseek:deepseek-chat"
    assert config.model_for("judge", cfg) == ("deepseek", "deepseek-chat")


def test_model_for_without_any_model_points_at_settings():
    with pytest.raises(config.ConfigError, match="afterwatch settings"):
        config.model_for("notes", config.load())


def test_model_for_rejects_unknown_provider():
    cfg = config.load()
    cfg["models"]["notes"] = "nope:model"
    with pytest.raises(config.ConfigError):
        config.model_for("notes", cfg)


def test_api_key_prefers_provider_env_then_afterwatch_env(monkeypatch):
    monkeypatch.setattr("keyring.get_password", lambda *a: "from-keyring")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AFTERWATCH_OPENAI_KEY", raising=False)
    cfg = config.load()
    assert config.api_key("openai", cfg) == "from-keyring"
    monkeypatch.setenv("AFTERWATCH_OPENAI_KEY", "aw")
    assert config.api_key("openai", cfg) == "aw"
    monkeypatch.setenv("OPENAI_API_KEY", "std")
    assert config.api_key("openai", cfg) == "std"


def test_api_key_survives_a_broken_keyring(monkeypatch):
    def broken(*a):
        raise RuntimeError("no backend")
    monkeypatch.setattr("keyring.get_password", broken)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("AFTERWATCH_XAI_KEY", raising=False)
    assert config.api_key("xai", config.load()) is None


def test_api_keys_never_land_in_the_config_file(monkeypatch):
    stored = {}
    monkeypatch.setattr("keyring.set_password", lambda s, p, k: stored.update({(s, p): k}))
    config.set_api_key("openai", "sk-secret")
    config.save(config.load())
    assert stored == {("afterwatch", "openai"): "sk-secret"}
    assert "sk-secret" not in config.config_path().read_text(encoding="utf-8")


def test_custom_folders_are_used_by_the_writer(tmp_path):
    from afterwatch import writer
    config.config_path().parent.mkdir(parents=True, exist_ok=True)
    config.config_path().write_text(
        '[folders]\nfailures = "Inbox/failed.md"\n', encoding="utf-8")
    (tmp_path / "Inbox").mkdir()
    writer.append_failure(tmp_path, "https://youtu.be/x", "private video")
    assert "youtu.be/x" in (tmp_path / "Inbox" / "failed.md").read_text(encoding="utf-8")
    assert config.folder("notes") == "Videos"
