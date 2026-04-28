"""Shared test fixtures."""

import subprocess
from pathlib import Path

import pytest

SAMPLE_TOML = """\
name = "test-session"

[tasks.server]
command = "echo 'Starting server...'"

[tasks.watcher]
command = "cargo watch -x check"
auto_start = false
"""

SAMPLE_TOML_WITH_HOOKS = """\
name = "hooked-session"

[hooks]
before_start = "echo global-before"
after_stop = "echo global-after"

[tasks.server]
command = "echo 'Starting server...'"

[tasks.server.hooks]
before_start = "echo server-before"

[tasks.watcher]
command = "cargo watch -x check"
auto_start = false
"""

SAMPLE_TOML_GLOBAL_AUTO_START_FALSE = """\
name = "lazy-session"
auto_start = false

[tasks.server]
command = "echo server"
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_toml(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML)
    return p


@pytest.fixture
def sample_toml_hooks(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML_WITH_HOOKS)
    return p


@pytest.fixture
def sample_toml_no_auto(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML_GLOBAL_AUTO_START_FALSE)
    return p


def _run_git(cwd: Path, *args: str) -> str:
    """Run git inside `cwd` and return stdout, raising on failure."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Bare-bones git repo with a single commit on `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "t@e.com")
    _run_git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("# repo\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def git_repo_with_worktree(git_repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A primary repo on `main` plus a linked worktree on `feature/fix-bug`.

    Returns (primary_path, linked_path).
    """
    linked = tmp_path / "fix-bug"
    _run_git(git_repo, "worktree", "add", "-b", "feature/fix-bug", str(linked))
    return git_repo, linked
