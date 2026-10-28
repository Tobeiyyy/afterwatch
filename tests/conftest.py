import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Every test gets its own empty config and state directory, so nothing
    touches the real one."""
    monkeypatch.setenv("AFTERWATCH_CONFIG", str(tmp_path / "afterwatch-config" / "config.toml"))
