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


# ---------------------------------------------------------------------------
# R-002 — daemon stop/restart SIGTERM→SIGKILL escalation
# ---------------------------------------------------------------------------


def _spawn_sleeping_child(*, trap_sigterm: bool):
    """Fork a long-sleeping subprocess and write its pid to GLOBAL_DAEMON_PID.

    Synchronizes on a "READY" stdout marker so SIGTERM races with handler
    installation don't pre-kill a child that's meant to trap SIGTERM.

    Returns the Popen handle — caller must `.wait()` it to reap the zombie
    after the test (otherwise `kill -0` still succeeds because the test
    runner is the child's parent rather than launchd/shell).
    """
    import subprocess
    import sys as _sys

    if trap_sigterm:
        script = (
            "import signal, sys, time;"
            "signal.signal(signal.SIGTERM, lambda *a: None);"
            "sys.stdout.write('READY\\n');sys.stdout.flush();"
            "time.sleep(60)"
        )
    else:
        script = (
            "import sys, time;"
            "sys.stdout.write('READY\\n');sys.stdout.flush();"
            "time.sleep(60)"
        )
    proc = subprocess.Popen(
        ["python3", "-c", script], stdout=subprocess.PIPE, text=True
    )
    # Block until child has installed its handler and reached time.sleep.
    line = proc.stdout.readline()
    if line.strip() != "READY":
        proc.kill()
        raise RuntimeError(f"child failed to signal ready: got {line!r}")
    paths_mod.GLOBAL_DAEMON_PID.write_text(str(proc.pid))
    _ = _sys  # unused
    return proc


def test_stop_escalates_to_sigkill_when_sigterm_ignored(isolated):
    """SIGTERM-trapping daemon → stop should escalate to SIGKILL and remove pidfile."""
    import os

    proc = _spawn_sleeping_child(trap_sigterm=True)
    try:
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["daemon", "stop", "--timeout", "0.5"])
        proc.wait(timeout=3.0)  # reap zombie so kill -0 starts returning ESRCH
        assert result.exit_code == 0, result.output
        assert "force-killed" in result.output
        with pytest.raises(OSError):
            os.kill(proc.pid, 0)
        assert not paths_mod.GLOBAL_DAEMON_PID.exists()
    finally:
        with contextlib_suppress():
            os.kill(proc.pid, 9)
        with contextlib_suppress():
            proc.wait(timeout=1.0)


def test_stop_uses_sigterm_when_daemon_exits_gracefully(isolated):
    """Daemon that respects SIGTERM → stop returns "term", no force-kill."""
    import os

    proc = _spawn_sleeping_child(trap_sigterm=False)
    try:
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["daemon", "stop", "--timeout", "3.0"])
        proc.wait(timeout=3.0)
        assert result.exit_code == 0, result.output
        assert "stopped" in result.output.lower()
        assert "force-killed" not in result.output
        with pytest.raises(OSError):
            os.kill(proc.pid, 0)
        assert not paths_mod.GLOBAL_DAEMON_PID.exists()
    finally:
        with contextlib_suppress():
            os.kill(proc.pid, 9)
        with contextlib_suppress():
            proc.wait(timeout=1.0)


def test_stop_no_daemon_running(isolated):
    """No pidfile → stop reports gracefully, doesn't crash."""
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["daemon", "stop"])
    assert result.exit_code == 0
    assert "No daemon running" in result.output


def test_stop_cleans_stale_pidfile_for_dead_process(isolated):
    """Pidfile points at a dead pid → escalation reports "already_gone" cleanly."""
    import os
    import subprocess

    proc = subprocess.Popen(["python3", "-c", "pass"])
    proc.wait()
    paths_mod.GLOBAL_DAEMON_PID.write_text(str(proc.pid))
    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["daemon", "stop"])
    # get_daemon_pid auto-removes stale pidfile, so the user sees "No daemon
    # running" rather than "already gone" — either is acceptable; assert no
    # crash + pidfile gone.
    assert result.exit_code == 0
    assert not paths_mod.GLOBAL_DAEMON_PID.exists()
    with pytest.raises(OSError):
        os.kill(proc.pid, 0)


def test_restart_escalates_then_spawns_fresh(isolated):
    """Restart against a SIGTERM-trapping daemon: SIGKILL, then new spawn."""
    import os
    from unittest.mock import MagicMock, patch

    proc = _spawn_sleeping_child(trap_sigterm=True)
    fake_new_pid = 99_999_999
    try:
        with (
            patch("subprocess.Popen", return_value=MagicMock(pid=fake_new_pid)),
            patch.object(cli_mod, "get_daemon_pid", side_effect=[proc.pid, fake_new_pid]),
        ):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["daemon", "restart", "--timeout", "0.5"])
        proc.wait(timeout=3.0)
        assert result.exit_code == 0, result.output
        assert "SIGKILL" in result.output or "restarted" in result.output.lower()
        with pytest.raises(OSError):
            os.kill(proc.pid, 0)
    finally:
        with contextlib_suppress():
            os.kill(proc.pid, 9)
        with contextlib_suppress():
            proc.wait(timeout=1.0)


def contextlib_suppress():
    import contextlib

    return contextlib.suppress(ProcessLookupError, OSError)
