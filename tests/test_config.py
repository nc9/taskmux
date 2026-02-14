"""Tests for functional TOML config module."""

from pathlib import Path

from taskmux.config import addTask, configExists, loadConfig, removeTask, writeConfig
from taskmux.models import TaskConfig, TaskmuxConfig


class TestConfigExists:
    def test_exists(self, sample_toml: Path):
        assert configExists(sample_toml) is True

    def test_missing(self, config_dir: Path):
        assert configExists(config_dir / "nope.toml") is False


class TestLoadConfig:
    def test_missing_returns_defaults(self, config_dir: Path):
        cfg = loadConfig(config_dir / "nope.toml")
        assert cfg.name == "taskmux"
        assert cfg.tasks == {}

    def test_parses_tasks(self, sample_toml: Path):
        cfg = loadConfig(sample_toml)
        assert cfg.name == "test-session"
        assert "server" in cfg.tasks
        assert cfg.tasks["server"].command == "echo 'Starting server...'"
        assert cfg.tasks["server"].auto_start is True

    def test_parses_auto_start_false(self, sample_toml: Path):
        cfg = loadConfig(sample_toml)
        assert cfg.tasks["watcher"].auto_start is False


class TestWriteConfig:
    def test_roundtrip(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="rt",
            tasks={
                "a": TaskConfig(command="echo a"),
                "b": TaskConfig(command="echo b", auto_start=False),
            },
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)

        loaded = loadConfig(p)
        assert loaded.name == "rt"
        assert loaded.tasks["a"].command == "echo a"
        assert loaded.tasks["a"].auto_start is True
        assert loaded.tasks["b"].auto_start is False

    def test_omits_default_auto_start(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={"t": TaskConfig(command="echo t")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert "auto_start" not in text


class TestAddTask:
    def test_persists(self, sample_toml: Path):
        cfg = addTask(sample_toml, "new-task", "echo new")
        assert "new-task" in cfg.tasks

        reloaded = loadConfig(sample_toml)
        assert "new-task" in reloaded.tasks
        assert reloaded.tasks["new-task"].command == "echo new"


class TestRemoveTask:
    def test_persists(self, sample_toml: Path):
        cfg, removed = removeTask(sample_toml, "server")
        assert removed is True
        assert "server" not in cfg.tasks

        reloaded = loadConfig(sample_toml)
        assert "server" not in reloaded.tasks

    def test_nonexistent_returns_false(self, sample_toml: Path):
        _, removed = removeTask(sample_toml, "ghost")
        assert removed is False
