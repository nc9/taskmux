"""Tests for v3 migration (per-project daemons → unified)."""

from __future__ import annotations

from unittest.mock import patch

from taskmux import paths as paths_mod


def _patch_layout(tmp_path):
    """Redirect every taskmux path to tmp_path for the test."""
    return patch.multiple(
        paths_mod,
        TASKMUX_DIR=tmp_path,
        EVENTS_FILE=tmp_path / "events.jsonl",
        PROJECTS_DIR=tmp_path / "projects",
        REGISTRY_PATH=tmp_path / "registry.json",
        GLOBAL_DAEMON_PID=tmp_path / "daemon.pid",
        GLOBAL_DAEMON_LOG=tmp_path / "daemon.log",
        _MIGRATION_MARKER=tmp_path / ".migrated-v2",
        _MIGRATION_MARKER_V3=tmp_path / ".migrated-v3",
    )


def test_v3_removes_per_project_daemon_files(tmp_path):
    with _patch_layout(tmp_path):
        # Pre-mark v2 done so v3 alone runs.
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".migrated-v2").touch()
        projects = tmp_path / "projects"
        for sess in ("alpha", "beta"):
            d = projects / sess
            d.mkdir(parents=True, exist_ok=True)
            (d / "daemon.pid").write_text("999999")  # stale pid
            (d / "daemon.log").write_text("noise\n")
            (d / "logs").mkdir()
            (d / "logs" / "task.log").write_text("kept\n")

        summary = paths_mod.migrate()
        assert summary["v3"] is True
        assert (tmp_path / ".migrated-v3").exists()

        for sess in ("alpha", "beta"):
            d = projects / sess
            assert not (d / "daemon.pid").exists()
            assert not (d / "daemon.log").exists()
            # Logs preserved
            assert (d / "logs" / "task.log").exists()


def test_v3_idempotent(tmp_path):
    with _patch_layout(tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / ".migrated-v2").touch()
        (tmp_path / ".migrated-v3").touch()
        summary = paths_mod.migrate()
        assert summary["v3"] is False


def test_v2_then_v3_run_together(tmp_path):
    with _patch_layout(tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        # Legacy v2 layout
        legacy_logs = tmp_path / "logs" / "alpha"
        legacy_logs.mkdir(parents=True)
        (legacy_logs / "task.log").write_text("legacy\n")
        # Legacy per-project daemon files (v3 target)
        proj = tmp_path / "projects" / "alpha"
        proj.mkdir(parents=True)
        (proj / "daemon.pid").write_text("999999")

        summary = paths_mod.migrate()
        assert summary["v2"] is True
        assert summary["v3"] is True
        # Logs migrated
        assert (tmp_path / "projects" / "alpha" / "logs" / "task.log").exists()
        # Per-project daemon.pid removed
        assert not (proj / "daemon.pid").exists()
