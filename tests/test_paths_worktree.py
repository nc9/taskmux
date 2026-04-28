"""Tests for worktree-aware path dispatch in taskmux/paths.py."""

from __future__ import annotations

from pathlib import Path

from taskmux import paths


def test_project_dir_primary(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    assert paths.projectDir("oddjob") == tmp_path / "projects" / "oddjob"


def test_project_dir_worktree(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    expected = tmp_path / "projects" / "oddjob" / "worktrees" / "fix-bug"
    assert paths.projectDir("oddjob", "fix-bug") == expected


def test_state_path_dispatch(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    assert paths.projectStatePath("oddjob").name == "state.json"
    assert paths.projectStatePath("oddjob", "fix-bug").parent.name == "fix-bug"


def test_log_paths_dispatch(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    primary = paths.taskLogPath("oddjob", "web")
    linked = paths.taskLogPath("oddjob", "web", "fix-bug")
    assert primary == tmp_path / "projects" / "oddjob" / "logs" / "web.log"
    assert linked == (
        tmp_path / "projects" / "oddjob" / "worktrees" / "fix-bug" / "logs" / "web.log"
    )


def test_cert_dir_keyed_by_project_id(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(paths, "CERTS_DIR", tmp_path / "certs")
    # Cert dir is keyed by project_id (not split by worktree) — caller passes
    # `oddjob-fix-bug` directly.
    assert paths.projectCertDir("oddjob-fix-bug") == tmp_path / "certs" / "oddjob-fix-bug"


def test_list_projects_lists_worktrees(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    # Real-world: primary state exists iff state.json or logs/ has been written.
    paths.ensureProjectDir("oddjob")
    paths.projectStatePath("oddjob").write_text("{}")
    paths.ensureProjectDir("oddjob", "fix-bug")
    paths.projectStatePath("oddjob", "fix-bug").write_text("{}")
    paths.ensureProjectDir("oddjob", "big-refactor")
    paths.projectStatePath("oddjob", "big-refactor").write_text("{}")
    paths.ensureProjectDir("other")
    paths.projectStatePath("other").write_text("{}")
    out = paths.listProjects()
    assert ("oddjob", None) in out
    assert ("oddjob", "fix-bug") in out
    assert ("oddjob", "big-refactor") in out
    assert ("other", None) in out


def test_list_projects_skips_pure_worktree_container(monkeypatch, tmp_path: Path):
    """A project dir with only a `worktrees/` subdir is a container, not a
    primary slot — should NOT appear as `(name, None)`."""
    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path / "projects")
    paths.ensureProjectDir("only-linked", "branch-a")
    out = paths.listProjects()
    assert ("only-linked", None) not in out
    assert ("only-linked", "branch-a") in out
