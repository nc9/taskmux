"""Tests for git worktree detection, slug rules, and ProjectIdentity."""

from __future__ import annotations

from pathlib import Path

from taskmux.config import loadProjectIdentity
from taskmux.worktree import (
    composeProjectId,
    computeWorktreeId,
    detectWorktree,
    slugifyBranch,
)


class TestSlugifyBranch:
    def test_strips_leading_segment(self):
        assert slugifyBranch("feature/fix-bug") == "fix-bug"

    def test_handles_uppercase(self):
        assert slugifyBranch("Fix/UI-Bug") == "ui-bug"

    def test_replaces_invalid_chars(self):
        assert slugifyBranch("hot fix!") == "hot-fix"

    def test_collapses_runs(self):
        assert slugifyBranch("foo--bar") == "foo-bar"

    def test_strips_ends(self):
        assert slugifyBranch("-foo-") == "foo"

    def test_truncates_with_sha_suffix(self):
        long = "f" * 80
        slug = slugifyBranch(long)
        assert len(slug) <= 63
        assert slug.endswith(slug.split("-")[-1])
        # last segment is the 6-char sha
        assert len(slug.split("-")[-1]) == 6


class TestDetectWorktree:
    def test_outside_repo_returns_none(self, tmp_path: Path):
        assert detectWorktree(tmp_path) is None

    def test_primary_worktree(self, git_repo: Path):
        info = detectWorktree(git_repo)
        assert info is not None
        assert info.is_linked is False
        assert info.branch == "main"
        assert info.path.resolve() == git_repo.resolve()
        assert info.primary_path.resolve() == git_repo.resolve()

    def test_linked_worktree(self, git_repo_with_worktree: tuple[Path, Path]):
        primary, linked = git_repo_with_worktree
        info = detectWorktree(linked)
        assert info is not None
        assert info.is_linked is True
        assert info.branch == "feature/fix-bug"
        assert info.path.resolve() == linked.resolve()
        assert info.primary_path.resolve() == primary.resolve()


class TestComputeWorktreeId:
    def test_primary_returns_none(self, git_repo: Path):
        info = detectWorktree(git_repo)
        assert info is not None
        assert computeWorktreeId(info) is None

    def test_linked_uses_branch_slug(self, git_repo_with_worktree: tuple[Path, Path]):
        _, linked = git_repo_with_worktree
        info = detectWorktree(linked)
        assert info is not None
        assert computeWorktreeId(info) == "fix-bug"

    def test_main_branch_in_linked_falls_back_to_dirname(self, git_repo: Path, tmp_path: Path):
        # Linked worktree explicitly on `main` (using a clone-like approach via
        # `git worktree add -B`) → fall back to dir basename.
        linked = tmp_path / "main-clone"
        # Can't have two worktrees on main without --force; use detached + fall through
        from tests.conftest import _run_git

        _run_git(git_repo, "worktree", "add", "--detach", str(linked))
        info = detectWorktree(linked)
        assert info is not None
        assert info.is_linked is True
        # Detached HEAD → branch is None → fallback to dir basename
        assert info.branch is None
        assert computeWorktreeId(info) == "main-clone"

    def test_sibling_slug_collision_appends_path_hash(self, git_repo: Path, tmp_path: Path):
        """Two linked worktrees whose branch names sanitize to the same slug
        get path-hash suffixes so their `project_id`s stay unique."""
        from tests.conftest import _run_git

        a = tmp_path / "a"
        b = tmp_path / "b"
        _run_git(git_repo, "worktree", "add", "-b", "feature/fix", str(a))
        _run_git(git_repo, "worktree", "add", "-b", "bug/fix", str(b))

        info_a = detectWorktree(a)
        info_b = detectWorktree(b)
        assert info_a is not None and info_b is not None

        id_a = computeWorktreeId(info_a)
        id_b = computeWorktreeId(info_b)
        assert id_a != id_b
        assert id_a is not None and id_a.startswith("fix-")
        assert id_b is not None and id_b.startswith("fix-")
        # Suffix is 6 hex chars
        assert len(id_a.rsplit("-", 1)[-1]) == 6
        assert len(id_b.rsplit("-", 1)[-1]) == 6


class TestComposeProjectId:
    def test_primary(self):
        assert composeProjectId("oddjob", None) == "oddjob"

    def test_linked(self):
        assert composeProjectId("oddjob", "fix-bug") == "oddjob-fix-bug"

    def test_custom_separator(self):
        assert composeProjectId("oddjob", "fix-bug", separator="__") == "oddjob__fix-bug"


class TestLoadProjectIdentity:
    def test_primary_yields_unsuffixed_id(self, git_repo: Path):
        cfg = git_repo / "taskmux.toml"
        cfg.write_text('name = "oddjob"\n')
        ident = loadProjectIdentity(cfg, cwd=git_repo)
        assert ident.project == "oddjob"
        assert ident.worktree_id is None
        assert ident.project_id == "oddjob"
        assert ident.branch == "main"

    def test_linked_yields_suffixed_id(self, git_repo_with_worktree: tuple[Path, Path]):
        primary, linked = git_repo_with_worktree
        # taskmux.toml lives in the primary repo and is shared by linked worktrees
        # via git worktree's default behavior (file is on the branch).
        cfg = primary / "taskmux.toml"
        cfg.write_text('name = "oddjob"\n')
        from tests.conftest import _run_git

        _run_git(primary, "add", "taskmux.toml")
        _run_git(primary, "commit", "-q", "-m", "add cfg")
        # Pull config into the linked branch's history so it exists there too.
        _run_git(linked, "merge", "main", "--no-edit")
        linked_cfg = linked / "taskmux.toml"
        assert linked_cfg.exists()
        ident = loadProjectIdentity(linked_cfg, cwd=linked)
        assert ident.project == "oddjob"
        assert ident.worktree_id == "fix-bug"
        assert ident.project_id == "oddjob-fix-bug"
        assert ident.branch == "feature/fix-bug"

    def test_disabled_via_config(self, git_repo_with_worktree: tuple[Path, Path]):
        _, linked = git_repo_with_worktree
        cfg = linked / "taskmux.toml"
        cfg.write_text('name = "oddjob"\n[worktree]\nenabled = false\n')
        ident = loadProjectIdentity(cfg, cwd=linked)
        assert ident.worktree_id is None
        assert ident.project_id == "oddjob"

    def test_outside_repo(self, tmp_path: Path):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "oddjob"\n')
        ident = loadProjectIdentity(cfg, cwd=tmp_path)
        assert ident.project_id == "oddjob"
        assert ident.worktree_id is None
        assert ident.branch is None


class TestWorktreeRowsFilter:
    """R-003: `taskmux worktree list` only shows current repo's entries."""

    def test_excludes_entries_with_no_detected_repo(
        self, git_repo: Path, tmp_path: Path, monkeypatch
    ):
        from taskmux import paths as paths_mod
        from taskmux.cli import _worktreeRowsForRepo
        from taskmux.registry import registerProject

        monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path / "tm")
        monkeypatch.setattr(paths_mod, "REGISTRY_PATH", tmp_path / "tm" / "registry.json")
        (tmp_path / "tm").mkdir()

        # Project A: in our git_repo
        cfg_a = git_repo / "taskmux.toml"
        cfg_a.write_text('name = "a"\n')
        registerProject("a", cfg_a)

        # Project B: outside any git repo
        outside = tmp_path / "outside"
        outside.mkdir()
        cfg_b = outside / "taskmux.toml"
        cfg_b.write_text('name = "b"\n')
        registerProject("b", cfg_b)

        # Caller is inside git_repo: only project A should show
        rows = _worktreeRowsForRepo(git_repo.resolve())
        sessions = sorted(r["session"] for r in rows)
        assert sessions == ["a"]

        # Caller has no repo: fallback shows everything
        rows = _worktreeRowsForRepo(None)
        sessions = sorted(r["session"] for r in rows)
        assert sessions == ["a", "b"]
