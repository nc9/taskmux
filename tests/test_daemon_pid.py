"""Tests for the global daemon PID file lifecycle."""

from __future__ import annotations

import os
from unittest.mock import patch

from taskmux import daemon as daemon_mod
from taskmux import paths as paths_mod


def _patch_taskmux_dir(tmp_path):
    """Redirect ~/.taskmux/ to tmp_path for a test."""
    return patch.object(paths_mod, "GLOBAL_DAEMON_PID", tmp_path / "daemon.pid")


def test_no_pid_file(tmp_path):
    with _patch_taskmux_dir(tmp_path):
        assert daemon_mod.get_daemon_pid() is None


def test_write_and_read(tmp_path):
    with _patch_taskmux_dir(tmp_path), patch.object(paths_mod, "TASKMUX_DIR", tmp_path):
        daemon_mod._write_daemon_pid()
        assert (tmp_path / "daemon.pid").exists()
        assert daemon_mod.get_daemon_pid() == os.getpid()


def test_clear_only_own_pid(tmp_path):
    with _patch_taskmux_dir(tmp_path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("1")  # foreign pid
        daemon_mod._clear_daemon_pid()
        assert pid_path.exists()


def test_stale_pid_cleaned(tmp_path):
    with _patch_taskmux_dir(tmp_path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("999999")
        assert daemon_mod.get_daemon_pid() is None
        assert not pid_path.exists()


def test_garbage_pid_returns_none(tmp_path):
    with _patch_taskmux_dir(tmp_path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("not-a-number")
        assert daemon_mod.get_daemon_pid() is None


def test_write_then_clear_own(tmp_path):
    with _patch_taskmux_dir(tmp_path), patch.object(paths_mod, "TASKMUX_DIR", tmp_path):
        daemon_mod._write_daemon_pid()
        daemon_mod._clear_daemon_pid()
        assert not (tmp_path / "daemon.pid").exists()
