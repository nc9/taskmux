"""Tests for cleanup.py — clean + prune pure functions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from taskmux import cleanup, paths


@pytest.fixture(autouse=True)
def _isolate_taskmux_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect ~/.taskmux/ into a tmp dir for every test in this file."""
    fake = tmp_path / "taskmux"
    monkeypatch.setattr(paths, "TASKMUX_DIR", fake)
    monkeypatch.setattr(paths, "EVENTS_FILE", fake / "events.jsonl")
    monkeypatch.setattr(paths, "PROJECTS_DIR", fake / "projects")
    monkeypatch.setattr(paths, "CERTS_DIR", fake / "certs")
    monkeypatch.setattr(paths, "REGISTRY_PATH", fake / "registry.json")
    monkeypatch.setattr(paths, "GLOBAL_DAEMON_PID", fake / "daemon.pid")
    monkeypatch.setattr(paths, "GLOBAL_DAEMON_LOG", fake / "daemon.log")
    monkeypatch.setattr(paths, "GLOBAL_CONFIG_PATH", fake / "config.toml")
    fake.mkdir(parents=True, exist_ok=True)
    yield fake


def _seedProject(project: str, worktree_id: str | None = None) -> Path:
    d = paths.ensureProjectDir(project, worktree_id)
    (d / "logs").mkdir(exist_ok=True)
    (d / "logs" / "server.log").write_text("hi\n")
    (d / "state.json").write_text(json.dumps({"assigned_ports": {"server": 5000}}))
    return d


class TestCleanProjectState:
    def test_wipes_logs_state_certs(self):
        d = _seedProject("alpha")
        cert_dir = paths.ensureProjectCertDir("alpha")
        (cert_dir / "cert.pem").write_text("x")

        with patch.object(cleanup, "_projectIsRunning", return_value=False):
            report = cleanup.cleanProjectState("alpha", None, "alpha")

        assert not (d / "logs").exists()
        assert not (d / "state.json").exists()
        assert not cert_dir.exists()
        assert any("logs" in p for p in report["deleted"])
        assert any("state.json" in p for p in report["deleted"])

    def test_dry_run_keeps_files(self):
        d = _seedProject("beta")
        with patch.object(cleanup, "_projectIsRunning", return_value=False):
            report = cleanup.cleanProjectState("beta", None, "beta", dry_run=True)
        assert (d / "logs" / "server.log").exists()
        assert (d / "state.json").exists()
        assert report["deleted"]

    def test_refuses_when_running(self):
        _seedProject("gamma")
        with patch.object(cleanup, "_projectIsRunning", return_value=True):
            report = cleanup.cleanProjectState("gamma", None, "gamma")
        assert report["deleted"] == []
        assert any("live windows" in s for s in report["skipped"])

    def test_force_overrides_running_check(self):
        d = _seedProject("delta")
        with patch.object(cleanup, "_projectIsRunning", return_value=True):
            report = cleanup.cleanProjectState("delta", None, "delta", force=True)
        assert not (d / "state.json").exists()
        assert report["deleted"]

    def test_primary_keeps_worktrees_subdir(self):
        _seedProject("epsilon")
        wt_dir = paths.ensureProjectDir("epsilon", "feature-x")
        (wt_dir / "state.json").write_text("{}")
        with patch.object(cleanup, "_projectIsRunning", return_value=False):
            cleanup.cleanProjectState("epsilon", None, "epsilon")
        assert wt_dir.exists()
        assert (wt_dir / "state.json").exists()


class TestCleanLogs:
    def test_per_task(self):
        d = _seedProject("a")
        (d / "logs" / "worker.log").write_text("hi")
        report = cleanup.cleanLogs("a", None, task="server")
        assert not (d / "logs" / "server.log").exists()
        assert (d / "logs" / "worker.log").exists()
        assert len(report["deleted"]) == 1

    def test_all_logs(self):
        d = _seedProject("b")
        report = cleanup.cleanLogs("b", None)
        assert not (d / "logs").exists()
        assert report["deleted"]


class TestCleanEvents:
    def test_truncate(self):
        paths.EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        paths.EVENTS_FILE.write_text('{"event":"x"}\n')
        report = cleanup.cleanEvents()
        assert paths.EVENTS_FILE.read_text() == ""
        assert report["deleted"]

    def test_missing_file_no_op(self):
        report = cleanup.cleanEvents()
        assert report["deleted"] == []


class TestCleanAll:
    def test_keeps_config_toml(self):
        paths.GLOBAL_CONFIG_PATH.write_text("api_port = 8765\n")
        _seedProject("p1")
        with patch("taskmux.daemon.get_daemon_pid", return_value=None):
            report = cleanup.cleanAll()
        assert paths.GLOBAL_CONFIG_PATH.exists()
        assert not paths.PROJECTS_DIR.exists()
        assert report["deleted"]

    def test_refuses_with_daemon_running(self):
        _seedProject("p2")
        with patch("taskmux.daemon.get_daemon_pid", return_value=12345):
            report = cleanup.cleanAll()
        assert report["deleted"] == []
        assert any("daemon" in s for s in report["skipped"])
        assert paths.PROJECTS_DIR.exists()


class TestFindOrphans:
    def test_stale_registry_when_config_missing(self, tmp_path: Path):
        from taskmux.registry import registerProject

        cfg = tmp_path / "ghost" / "taskmux.toml"
        cfg.parent.mkdir()
        cfg.write_text('name = "ghost"\n')
        registerProject("ghost", cfg)
        cfg.unlink()
        cfg.parent.rmdir()

        with patch("taskmux.cleanup._liveTmuxSessions", return_value=set()):
            report = cleanup.findOrphans()

        assert any(s["session"] == "ghost" for s in report["stale_registry"])

    def test_orphan_log_dir(self):
        _seedProject("nobody")
        with patch("taskmux.cleanup._liveTmuxSessions", return_value=set()):
            report = cleanup.findOrphans()
        assert "nobody" in report["orphan_log_dirs"]

    def test_stale_daemon_pid(self):
        paths.GLOBAL_DAEMON_PID.write_text("999999")
        with (
            patch("taskmux.cleanup._liveTmuxSessions", return_value=set()),
            patch("taskmux.daemon.get_daemon_pid", return_value=None),
        ):
            report = cleanup.findOrphans()
        assert report["stale_daemon_pid"] == 999999


class TestApplyPrune:
    def test_unregisters_stale_entries(self, tmp_path: Path):
        from taskmux.registry import readRegistry, registerProject

        cfg = tmp_path / "vanished" / "taskmux.toml"
        cfg.parent.mkdir()
        cfg.write_text('name = "vanished"\n')
        registerProject("vanished", cfg)
        cfg.unlink()

        report = cleanup.findOrphans()
        actions = cleanup.applyPrune(report)
        assert "vanished" in actions["unregistered"]
        assert "vanished" not in readRegistry()

    def test_removes_stale_pidfile(self):
        paths.GLOBAL_DAEMON_PID.write_text("999999")
        with patch("taskmux.daemon.get_daemon_pid", return_value=None):
            report = cleanup.findOrphans()
        actions = cleanup.applyPrune(report)
        assert actions["removed_pidfile"]
        assert not paths.GLOBAL_DAEMON_PID.exists()
