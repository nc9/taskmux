"""Tests for taskmux.validate — environment lint beyond Pydantic structure."""

from __future__ import annotations

from pathlib import Path

from taskmux.models import TaskConfig, TaskmuxConfig
from taskmux.validate import _command_head, validateEnvironment


def _cfg(tasks: dict) -> TaskmuxConfig:
    parsed = {
        k: TaskConfig(**v) if isinstance(v, dict) else TaskConfig(command=v)
        for k, v in tasks.items()
    }
    return TaskmuxConfig(name="lint-test", tasks=parsed)


class TestCwdCheck:
    def test_missing_cwd_is_error(self, tmp_path: Path):
        cfg = _cfg({"web": {"command": "true", "cwd": str(tmp_path / "gone")}})
        issues = validateEnvironment(cfg, tmp_path)
        assert any(i.code == "cwd_missing" and i.severity == "error" for i in issues)

    def test_relative_cwd_resolved_against_config_dir(self, tmp_path: Path):
        (tmp_path / "apps" / "web").mkdir(parents=True)
        cfg = _cfg({"web": {"command": "true", "cwd": "apps/web"}})
        assert validateEnvironment(cfg, tmp_path) == []

    def test_relative_cwd_missing(self, tmp_path: Path):
        cfg = _cfg({"web": {"command": "true", "cwd": "apps/gone"}})
        issues = validateEnvironment(cfg, tmp_path)
        assert [i.code for i in issues if i.severity == "error"] == ["cwd_missing"]

    def test_no_cwd_ok(self, tmp_path: Path):
        cfg = _cfg({"web": "true"})
        assert validateEnvironment(cfg, tmp_path) == []


class TestCommandHead:
    def test_simple(self):
        assert _command_head("bun run dev") == "bun"

    def test_env_prefix_stripped(self):
        assert _command_head("PORT=4005 bun run dev") == "bun"

    def test_shell_meta_skipped(self):
        assert _command_head("foo && bar") is None
        assert _command_head("foo | bar") is None
        assert _command_head("echo $HOME") is None

    def test_only_env_assignments(self):
        assert _command_head("FOO=bar") is None


class TestCommandCheck:
    def test_missing_executable_warns(self, tmp_path: Path):
        cfg = _cfg({"web": "definitely-not-a-real-binary-xyz run dev"})
        issues = validateEnvironment(cfg, tmp_path)
        assert [i.code for i in issues] == ["command_not_found"]
        assert issues[0].severity == "warning"

    def test_existing_executable_ok(self, tmp_path: Path):
        cfg = _cfg({"web": "sh -c 'echo hi'"})  # quotes parsed by shlex, head is `sh`
        cfg2 = _cfg({"web": "sleep 5"})
        assert validateEnvironment(cfg, tmp_path) == []
        assert validateEnvironment(cfg2, tmp_path) == []

    def test_quoted_args_still_checked(self, tmp_path: Path):
        """Quotes must not disable the heuristic — they're common in real
        commands and shlex handles them fine."""
        cfg = _cfg({"web": 'definitely-not-a-real-binary-xyz --name "my app"'})
        issues = validateEnvironment(cfg, tmp_path)
        assert [i.code for i in issues] == ["command_not_found"]

    def test_builtin_ok(self, tmp_path: Path):
        cfg = _cfg({"web": "true"})
        assert validateEnvironment(cfg, tmp_path) == []

    def test_relative_path_executable_against_cwd(self, tmp_path: Path):
        (tmp_path / "bin").mkdir()
        script = tmp_path / "bin" / "run.sh"
        script.write_text("#!/bin/sh\n")
        cfg = _cfg({"web": {"command": "./bin/run.sh", "cwd": str(tmp_path)}})
        assert validateEnvironment(cfg, tmp_path) == []
        cfg_missing = _cfg({"web": {"command": "./bin/gone.sh", "cwd": str(tmp_path)}})
        issues = validateEnvironment(cfg_missing, tmp_path)
        assert [i.code for i in issues] == ["command_not_found"]


class TestHealthUrl:
    def test_invalid_scheme_warns(self, tmp_path: Path):
        cfg = _cfg({"web": {"command": "true", "health_url": "localhost:3000/health"}})
        issues = validateEnvironment(cfg, tmp_path)
        assert [i.code for i in issues] == ["health_url_invalid"]

    def test_valid_url_ok(self, tmp_path: Path):
        cfg = _cfg({"web": {"command": "true", "health_url": "http://localhost:3000/health"}})
        assert validateEnvironment(cfg, tmp_path) == []


class TestDeps:
    def test_auto_start_dep_on_manual_task_warns(self, tmp_path: Path):
        cfg = _cfg(
            {
                "db": {"command": "true", "auto_start": False},
                "web": {"command": "true", "depends_on": ["db"]},
            }
        )
        issues = validateEnvironment(cfg, tmp_path)
        assert [i.code for i in issues] == ["dep_not_auto_started"]

    def test_both_auto_start_ok(self, tmp_path: Path):
        cfg = _cfg(
            {
                "db": {"command": "true"},
                "web": {"command": "true", "depends_on": ["db"]},
            }
        )
        assert validateEnvironment(cfg, tmp_path) == []


class TestOrdering:
    def test_errors_sort_first(self, tmp_path: Path):
        cfg = _cfg(
            {
                "a": {"command": "definitely-not-a-real-binary-xyz"},
                "b": {"command": "true", "cwd": str(tmp_path / "gone")},
            }
        )
        issues = validateEnvironment(cfg, tmp_path)
        assert issues[0].severity == "error"
        assert issues[0].code == "cwd_missing"
