"""Tests for ~/.taskmux/config.toml host-wide config."""

from __future__ import annotations

import warnings

import pytest

from taskmux import global_config as gc
from taskmux import paths as paths_mod
from taskmux.errors import ErrorCode, TaskmuxError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "GLOBAL_CONFIG_PATH", tmp_path / "config.toml")
    return tmp_path


def test_missing_returns_defaults(isolated):
    cfg = gc.loadGlobalConfig()
    assert cfg.health_check_interval == 30
    assert cfg.api_port == 8765


def test_partial_override(isolated):
    (isolated / "config.toml").write_text("health_check_interval = 5\n")
    cfg = gc.loadGlobalConfig()
    assert cfg.health_check_interval == 5
    assert cfg.api_port == 8765  # unchanged


def test_full_override(isolated):
    (isolated / "config.toml").write_text("health_check_interval = 60\napi_port = 9999\n")
    cfg = gc.loadGlobalConfig()
    assert cfg.health_check_interval == 60
    assert cfg.api_port == 9999


def test_unknown_key_warns_and_drops(isolated):
    (isolated / "config.toml").write_text("health_check_interval = 5\nspeculative_setting = true\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = gc.loadGlobalConfig()
        assert any("speculative_setting" in str(w.message) for w in caught)
    assert cfg.health_check_interval == 5


def test_invalid_value_raises(isolated):
    (isolated / "config.toml").write_text("health_check_interval = 0\n")
    with pytest.raises(TaskmuxError) as excinfo:
        gc.loadGlobalConfig()
    assert excinfo.value.code is ErrorCode.CONFIG_VALIDATION


def test_corrupt_toml_raises(isolated):
    (isolated / "config.toml").write_text("not = valid = toml\n")
    with pytest.raises(TaskmuxError) as excinfo:
        gc.loadGlobalConfig()
    assert excinfo.value.code is ErrorCode.CONFIG_PARSE_ERROR


def test_write_roundtrip(isolated):
    cfg = gc.GlobalConfig(health_check_interval=15, api_port=8800)
    gc.writeGlobalConfig(cfg)
    reloaded = gc.loadGlobalConfig()
    assert reloaded.health_check_interval == 15
    assert reloaded.api_port == 8800


def test_update_merges(isolated):
    (isolated / "config.toml").write_text("api_port = 9001\n")
    new = gc.updateGlobalConfig({"health_check_interval": 12})
    assert new.health_check_interval == 12
    assert new.api_port == 9001
    on_disk = gc.loadGlobalConfig()
    assert on_disk.health_check_interval == 12
    assert on_disk.api_port == 9001


# ---- [mcp] block ----


def test_mcp_defaults(isolated):
    cfg = gc.loadGlobalConfig()
    assert cfg.mcp.enabled is True
    assert cfg.mcp.path == "/mcp"
    assert cfg.mcp.filter == []


def test_mcp_partial_override(isolated):
    (isolated / "config.toml").write_text(
        '[mcp]\nenabled = false\nfilter = ["task_exited", "auto_restart"]\n'
    )
    cfg = gc.loadGlobalConfig()
    assert cfg.mcp.enabled is False
    assert cfg.mcp.path == "/mcp"
    assert cfg.mcp.filter == ["task_exited", "auto_restart"]


def test_mcp_invalid_path_rejected(isolated):
    (isolated / "config.toml").write_text('[mcp]\npath = "mcp"\n')
    with pytest.raises(TaskmuxError) as excinfo:
        gc.loadGlobalConfig()
    assert excinfo.value.code is ErrorCode.CONFIG_VALIDATION


def test_mcp_default_section_omitted_on_write(isolated):
    cfg = gc.GlobalConfig()
    gc.writeGlobalConfig(cfg)
    text = (isolated / "config.toml").read_text()
    assert "[mcp]" not in text


def test_mcp_overridden_section_round_trips(isolated):
    cfg = gc.GlobalConfig(mcp=gc.McpGlobalConfig(enabled=False, filter=["task_exited"]))
    gc.writeGlobalConfig(cfg)
    reloaded = gc.loadGlobalConfig()
    assert reloaded.mcp.enabled is False
    assert reloaded.mcp.filter == ["task_exited"]
    assert reloaded.mcp.path == "/mcp"
