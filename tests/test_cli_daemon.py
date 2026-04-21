"""Targeted tests for daemon CLI plumbing (port forwarding, config-set guard)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from taskmux import cli as cli_mod
from taskmux import paths as paths_mod
from taskmux.errors import ErrorCode, TaskmuxError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(paths_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(paths_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_PID", tmp_path / "daemon.pid")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_LOG", tmp_path / "daemon.log")
    monkeypatch.setattr(paths_mod, "GLOBAL_CONFIG_PATH", tmp_path / "config.toml")
    return tmp_path


# ---------------------------------------------------------------------------
# R-001 — port plumbing
# ---------------------------------------------------------------------------


def test_spawn_detached_omits_port_when_unset(isolated):
    """No port arg → bare `python -m taskmux daemon` (daemon resolves from config)."""
    fake_proc = MagicMock(pid=12345)
    with (
        patch("subprocess.Popen", return_value=fake_proc) as popen,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
    ):
        cli_mod._spawn_detached_daemon(port=None)
    cmd = popen.call_args.args[0]
    assert cmd[-2:] != ["--port", str(cmd[-1])]
    assert "--port" not in cmd


def test_spawn_detached_forwards_port(isolated):
    """Explicit port → appended as `--port <port>` to the spawn command."""
    fake_proc = MagicMock(pid=12345)
    with (
        patch("subprocess.Popen", return_value=fake_proc) as popen,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
    ):
        cli_mod._spawn_detached_daemon(port=9999)
    cmd = popen.call_args.args[0]
    assert "--port" in cmd
    assert cmd[cmd.index("--port") + 1] == "9999"


def test_daemon_list_uses_configured_port(isolated):
    """daemon list with no --port reads api_port from global config."""
    (isolated / "config.toml").write_text("api_port = 9876\n")
    runner = CliRunner()
    with (
        patch.object(cli_mod, "get_daemon_pid", return_value=42),
        patch.object(cli_mod, "_query_live_projects", return_value={}) as q,
    ):
        # Need at least one registered project so the query is invoked
        from taskmux import registry as reg

        cfg = isolated / "alpha" / "taskmux.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('name = "alpha"\n')
        reg.registerProject("alpha", cfg)
        result = runner.invoke(cli_mod.app, ["daemon", "list"])
    assert result.exit_code == 0
    assert q.called
    # _query_live_projects(port=...) was called with api_port from config.toml
    assert q.call_args.kwargs.get("port") == 9876


def test_daemon_list_explicit_port_wins(isolated):
    """--port on `daemon list` overrides the configured api_port."""
    (isolated / "config.toml").write_text("api_port = 9876\n")
    runner = CliRunner()
    with (
        patch.object(cli_mod, "get_daemon_pid", return_value=42),
        patch.object(cli_mod, "_query_live_projects", return_value={}) as q,
    ):
        from taskmux import registry as reg

        cfg = isolated / "alpha" / "taskmux.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text('name = "alpha"\n')
        reg.registerProject("alpha", cfg)
        result = runner.invoke(cli_mod.app, ["daemon", "list", "--port", "1234"])
    assert result.exit_code == 0
    assert q.call_args.kwargs.get("port") == 1234


# ---------------------------------------------------------------------------
# R-003 — unknown global config key rejected
# ---------------------------------------------------------------------------


def test_config_set_rejects_unknown_key(isolated):
    """Unknown key surfaces as TaskmuxError(CONFIG_VALIDATION)."""
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["config", "set", "speculative_setting", "1"])
    assert result.exit_code != 0
    assert isinstance(result.exception, TaskmuxError)
    assert result.exception.code is ErrorCode.CONFIG_VALIDATION
    assert "speculative_setting" in result.exception.message


def test_config_set_unknown_key_does_not_write_file(isolated):
    """A rejected key must not create or modify config.toml."""
    cfg_path = isolated / "config.toml"
    assert not cfg_path.exists()
    runner = CliRunner()
    runner.invoke(cli_mod.app, ["config", "set", "no_such_key", "1"])
    assert not cfg_path.exists()


def test_config_set_accepts_known_key(isolated):
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["config", "set", "health_check_interval", "12"])
    assert result.exit_code == 0
    from taskmux.global_config import loadGlobalConfig

    assert loadGlobalConfig().health_check_interval == 12
