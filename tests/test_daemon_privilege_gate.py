"""Issue 1 — refuse a non-root (re)spawn of a root-bootstrapped daemon.

A daemon started via sudo binds :443/:80 (+ system DNS) as root then drops to
the invoking user. A later non-root start/stop/restart that *replaces* it comes
up unprivileged and fails the fatal :443 bind, breaking every *.localhost URL.
These tests pin the refuse-only gate that prevents that broken state.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from taskmux import cli as cli_mod
from taskmux import ipc_client
from taskmux import paths as paths_mod
from taskmux.errors import TaskmuxError
from taskmux.global_config import GlobalConfig, privilegedNeeds, requiresRoot


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(paths_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(paths_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_PID", tmp_path / "daemon.pid")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_LOG", tmp_path / "daemon.log")
    monkeypatch.setattr(paths_mod, "GLOBAL_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.delenv("TASKMUX_DISABLE_PROXY", raising=False)
    monkeypatch.delenv("TASKMUX_ALLOW_UNPRIVILEGED", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# privilegedNeeds / requiresRoot — the single source of truth
# ---------------------------------------------------------------------------


def test_default_config_needs_root():
    """Default proxy on :443 + etc_hosts → root required."""
    needs = privilegedNeeds(GlobalConfig())
    assert "bind :443" in needs
    assert any("/etc/hosts" in n for n in needs)
    assert requiresRoot(GlobalConfig())


def test_redirect_port_below_1024_needs_root():
    cfg = GlobalConfig(proxy_https_port=8443, proxy_http_redirect_port=80, host_resolver="noop")
    assert privilegedNeeds(cfg) == ["bind :80"]


def test_dns_server_resolver_needs_root():
    cfg = GlobalConfig(
        proxy_https_port=8443,
        proxy_http_redirect_port=0,
        host_resolver="dns_server",
        dns_managed_tld="test",
    )
    assert privilegedNeeds(cfg) == ["write /etc/resolver/test"]


def test_no_root_needed_when_proxy_disabled():
    assert privilegedNeeds(GlobalConfig(proxy_enabled=False)) == []
    assert not requiresRoot(GlobalConfig(proxy_enabled=False))


def test_no_root_needed_with_high_ports_and_noop_resolver():
    cfg = GlobalConfig(proxy_https_port=8443, proxy_http_redirect_port=0, host_resolver="noop")
    assert privilegedNeeds(cfg) == []


def test_disable_proxy_env_short_circuits(monkeypatch):
    monkeypatch.setenv("TASKMUX_DISABLE_PROXY", "1")
    assert privilegedNeeds(GlobalConfig()) == []


# ---------------------------------------------------------------------------
# CLI gate — daemon start / restart
# ---------------------------------------------------------------------------


def test_restart_refuses_non_root_and_does_not_stop(isolated, monkeypatch):
    """The crux: refuse BEFORE stopping, so a working root daemon isn't killed."""
    monkeypatch.setattr(cli_mod, "_is_root", lambda: False)
    runner = CliRunner()
    with (
        patch.object(cli_mod, "_stop_daemon_with_escalation") as stop,
        patch.object(cli_mod, "_spawn_detached_daemon") as spawn,
        patch.object(cli_mod, "get_daemon_pid", return_value=4242),
    ):
        result = runner.invoke(cli_mod.app, ["daemon", "restart"])
    assert result.exit_code == 1
    stop.assert_not_called()
    spawn.assert_not_called()
    assert "sudo taskmux daemon restart" in result.output


def test_start_refuses_non_root(isolated, monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_root", lambda: False)
    runner = CliRunner()
    with (
        patch.object(cli_mod, "_spawn_detached_daemon") as spawn,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
        patch.object(cli_mod, "_autoRegisterCwd"),
    ):
        result = runner.invoke(cli_mod.app, ["daemon", "start"])
    assert result.exit_code == 1
    spawn.assert_not_called()
    assert "sudo taskmux daemon start" in result.output


def test_start_force_bypasses_gate_and_propagates(isolated, monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_root", lambda: False)
    runner = CliRunner()
    with (
        patch.object(cli_mod, "_spawn_detached_daemon", return_value=123) as spawn,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
        patch.object(cli_mod, "_autoRegisterCwd"),
        patch.object(cli_mod, "_warn_port_conflict"),
    ):
        result = runner.invoke(cli_mod.app, ["daemon", "start", "--force"])
    assert result.exit_code == 0
    spawn.assert_called_once()
    assert spawn.call_args.kwargs.get("allow_unprivileged") is True


def test_start_proceeds_when_no_root_needed(isolated, monkeypatch):
    (isolated / "config.toml").write_text("proxy_enabled = false\n")
    monkeypatch.setattr(cli_mod, "_is_root", lambda: False)
    runner = CliRunner()
    with (
        patch.object(cli_mod, "_spawn_detached_daemon", return_value=123) as spawn,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
        patch.object(cli_mod, "_autoRegisterCwd"),
        patch.object(cli_mod, "_warn_port_conflict"),
    ):
        result = runner.invoke(cli_mod.app, ["daemon", "start"])
    assert result.exit_code == 0
    spawn.assert_called_once()


def test_root_caller_is_not_gated(isolated, monkeypatch):
    monkeypatch.setattr(cli_mod, "_is_root", lambda: True)
    runner = CliRunner()
    with (
        patch.object(cli_mod, "_spawn_detached_daemon", return_value=7) as spawn,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
        patch.object(cli_mod, "_autoRegisterCwd"),
        patch.object(cli_mod, "_warn_port_conflict"),
    ):
        result = runner.invoke(cli_mod.app, ["daemon", "start"])
    assert result.exit_code == 0
    spawn.assert_called_once()


# ---------------------------------------------------------------------------
# _spawn_detached_daemon — env propagation of --force
# ---------------------------------------------------------------------------


def test_spawn_detached_sets_allow_unprivileged_env(isolated):
    with (
        patch("subprocess.Popen", return_value=MagicMock(pid=1)) as popen,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
    ):
        cli_mod._spawn_detached_daemon(allow_unprivileged=True)
    env = popen.call_args.kwargs.get("env")
    assert env is not None
    assert env.get("TASKMUX_ALLOW_UNPRIVILEGED") == "1"


def test_spawn_detached_inherits_env_by_default(isolated):
    with (
        patch("subprocess.Popen", return_value=MagicMock(pid=1)) as popen,
        patch.object(cli_mod, "get_daemon_pid", return_value=None),
    ):
        cli_mod._spawn_detached_daemon()
    assert popen.call_args.kwargs.get("env") is None


# ---------------------------------------------------------------------------
# IPC auto-spawn gate
# ---------------------------------------------------------------------------


def test_autospawn_helper_raises_for_non_root(isolated, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(TaskmuxError) as ei:
        ipc_client._refuse_unprivileged_autospawn()
    assert "sudo taskmux daemon" in ei.value.message


def test_autospawn_helper_silent_for_root(isolated, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    ipc_client._refuse_unprivileged_autospawn()  # no raise


def test_autospawn_helper_silent_with_force_env(isolated, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv("TASKMUX_ALLOW_UNPRIVILEGED", "1")
    ipc_client._refuse_unprivileged_autospawn()  # no raise


def test_autospawn_helper_silent_when_no_root_needed(isolated, monkeypatch):
    (isolated / "config.toml").write_text("proxy_enabled = false\n")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    ipc_client._refuse_unprivileged_autospawn()  # no raise


def test_ensure_daemon_running_refuses_before_spawn(isolated, monkeypatch):
    """No live daemon + needs root + non-root → raise, never Popen."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with (
        patch("taskmux.daemon.get_daemon_pid", return_value=None),
        patch("subprocess.Popen") as popen,
        pytest.raises(TaskmuxError),
    ):
        ipc_client.ensure_daemon_running()
    popen.assert_not_called()
