"""Tests for daemon PID file lifecycle helpers."""

from __future__ import annotations

import os
from unittest.mock import patch

from taskmux import daemon as daemon_mod


def test_no_pid_file(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    with patch.object(daemon_mod, "DAEMON_PID_PATH", pid_path):
        assert daemon_mod.get_daemon_pid() is None


def test_write_and_read(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    with patch.object(daemon_mod, "DAEMON_PID_PATH", pid_path):
        daemon_mod._write_daemon_pid()
        assert pid_path.exists()
        assert daemon_mod.get_daemon_pid() == os.getpid()


def test_clear_only_own_pid(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    with patch.object(daemon_mod, "DAEMON_PID_PATH", pid_path):
        # Foreign PID — must not delete
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("1")  # init, never matches our pid
        daemon_mod._clear_daemon_pid()
        assert pid_path.exists()


def test_stale_pid_cleaned(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # Pick a PID that almost certainly doesn't exist
    pid_path.write_text("999999")
    with patch.object(daemon_mod, "DAEMON_PID_PATH", pid_path):
        assert daemon_mod.get_daemon_pid() is None
    assert not pid_path.exists()


def test_garbage_pid_returns_none(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("not-a-number")
    with patch.object(daemon_mod, "DAEMON_PID_PATH", pid_path):
        assert daemon_mod.get_daemon_pid() is None


def test_write_then_clear_own(tmp_path):
    pid_path = tmp_path / "daemon.pid"
    with patch.object(daemon_mod, "DAEMON_PID_PATH", pid_path):
        daemon_mod._write_daemon_pid()
        daemon_mod._clear_daemon_pid()
        assert not pid_path.exists()
