"""Tests for functional TOML config module."""

from pathlib import Path

from taskmux.config import addTask, configExists, loadConfig, removeTask, writeConfig
from taskmux.models import (
    HookConfig,
    RestartPolicy,
    TaskConfig,
    TaskmuxConfig,
    WorktreeConfig,
)


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

    def test_parses_global_hooks(self, sample_toml_hooks: Path):
        cfg = loadConfig(sample_toml_hooks)
        assert cfg.hooks.before_start == "echo global-before"
        assert cfg.hooks.after_stop == "echo global-after"
        assert cfg.hooks.after_start is None

    def test_parses_task_hooks(self, sample_toml_hooks: Path):
        cfg = loadConfig(sample_toml_hooks)
        assert cfg.tasks["server"].hooks.before_start == "echo server-before"
        assert cfg.tasks["server"].hooks.after_start is None

    def test_parses_global_auto_start_false(self, sample_toml_no_auto: Path):
        cfg = loadConfig(sample_toml_no_auto)
        assert cfg.auto_start is False
        assert cfg.name == "lazy-session"

    def test_default_global_auto_start_true(self, sample_toml: Path):
        cfg = loadConfig(sample_toml)
        assert cfg.auto_start is True


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

    def test_roundtrip_hooks(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="hooked",
            hooks=HookConfig(before_start="echo pre", after_stop="echo post"),
            tasks={
                "srv": TaskConfig(
                    command="echo srv",
                    hooks=HookConfig(before_start="echo srv-pre"),
                ),
            },
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)

        loaded = loadConfig(p)
        assert loaded.hooks.before_start == "echo pre"
        assert loaded.hooks.after_stop == "echo post"
        assert loaded.hooks.after_start is None
        assert loaded.tasks["srv"].hooks.before_start == "echo srv-pre"

    def test_omits_empty_hooks(self, config_dir: Path):
        cfg = TaskmuxConfig(name="x", tasks={"t": TaskConfig(command="echo t")})
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert "[hooks]" not in text

    def test_writes_global_auto_start_false(self, config_dir: Path):
        cfg = TaskmuxConfig(name="x", auto_start=False)
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert "auto_start = false" in text

    def test_roundtrip_global_auto_start_false(self, config_dir: Path):
        cfg = TaskmuxConfig(name="x", auto_start=False, tasks={"a": TaskConfig(command="echo a")})
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.auto_start is False


class TestWriteConfigNewFields:
    def test_roundtrip_cwd(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={"api": TaskConfig(command="cargo run", cwd="apps/api")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.tasks["api"].cwd == "apps/api"

    def test_roundtrip_health_check(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={
                "db": TaskConfig(command="docker up", health_check="pg_isready", health_interval=5),
            },
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.tasks["db"].health_check == "pg_isready"
        assert loaded.tasks["db"].health_interval == 5

    def test_roundtrip_depends_on(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={
                "db": TaskConfig(command="echo db"),
                "api": TaskConfig(command="echo api", depends_on=["db"]),
            },
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.tasks["api"].depends_on == ["db"]

    def test_omits_default_new_fields(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={"t": TaskConfig(command="echo t")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert "cwd" not in text
        assert "health_check" not in text
        assert "health_interval" not in text
        assert "depends_on" not in text
        assert "restart_policy" not in text

    def test_omits_default_restart_policy(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={"t": TaskConfig(command="echo t", restart_policy="on-failure")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert "restart_policy" not in text

    def test_writes_non_default_restart_policy(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={
                "a": TaskConfig(command="echo a", restart_policy="no"),
                "b": TaskConfig(command="echo b", restart_policy="always"),
            },
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert 'restart_policy = "no"' in text
        assert 'restart_policy = "always"' in text

    def test_roundtrip_restart_policy(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            tasks={
                "a": TaskConfig(command="echo a", restart_policy="no"),
                "b": TaskConfig(command="echo b", restart_policy="always"),
                "c": TaskConfig(command="echo c"),  # default on-failure
            },
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.tasks["a"].restart_policy == RestartPolicy.NO
        assert loaded.tasks["b"].restart_policy == RestartPolicy.ALWAYS
        assert loaded.tasks["c"].restart_policy == RestartPolicy.ON_FAILURE


class TestWriteConfigHostSentinels:
    """R-001: apex (`@`) must round-trip back to `@` in TOML so the validator
    accepts the rewritten file. Wildcard (`*`) round-trips trivially."""

    def test_apex_round_trips(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="postpiece",
            tasks={"website": TaskConfig(command="echo w", host="@")},
        )
        # In-memory: normalised to ""
        assert cfg.tasks["website"].host == ""
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert 'host = "@"' in text
        assert 'host = ""' not in text  # would fail load
        loaded = loadConfig(p)
        assert loaded.tasks["website"].host == ""

    def test_wildcard_round_trips(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="postpiece",
            tasks={"frontloader": TaskConfig(command="echo f", host="*")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.tasks["frontloader"].host == "*"

    def test_apex_via_addTask_round_trips(self, sample_toml: Path):
        from taskmux.config import addTask

        addTask(sample_toml, "website", "echo w", host="@")
        loaded = loadConfig(sample_toml)
        assert loaded.tasks["website"].host == ""
        # Sanity: file is itself reload-safe.
        loadConfig(sample_toml)


class TestWriteConfigWorktreeRoundtrip:
    def test_omits_table_when_all_defaults(self, config_dir: Path):
        cfg = TaskmuxConfig(name="x", tasks={"a": TaskConfig(command="echo a")})
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        assert "[worktree]" not in p.read_text()

    def test_persists_disabled(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            worktree=WorktreeConfig(enabled=False),
            tasks={"a": TaskConfig(command="echo a")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        text = p.read_text()
        assert "[worktree]" in text
        assert "enabled = false" in text
        loaded = loadConfig(p)
        assert loaded.worktree.enabled is False

    def test_persists_custom_separator_and_main_branches(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            worktree=WorktreeConfig(separator="--", main_branches=["trunk"]),
            tasks={"a": TaskConfig(command="echo a")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        loaded = loadConfig(p)
        assert loaded.worktree.separator == "--"
        assert loaded.worktree.main_branches == ["trunk"]

    def test_addTask_preserves_worktree_disabled(self, config_dir: Path):
        cfg = TaskmuxConfig(
            name="x",
            worktree=WorktreeConfig(enabled=False),
            tasks={"a": TaskConfig(command="echo a")},
        )
        p = config_dir / "taskmux.toml"
        writeConfig(p, cfg)
        addTask(p, "new", "echo new")
        reloaded = loadConfig(p)
        assert reloaded.worktree.enabled is False
        assert "new" in reloaded.tasks


class TestAddTask:
    def test_persists(self, sample_toml: Path):
        cfg = addTask(sample_toml, "new-task", "echo new")
        assert "new-task" in cfg.tasks

        reloaded = loadConfig(sample_toml)
        assert "new-task" in reloaded.tasks
        assert reloaded.tasks["new-task"].command == "echo new"

    def test_preserves_hooks(self, sample_toml_hooks: Path):
        addTask(sample_toml_hooks, "new-task", "echo new")
        reloaded = loadConfig(sample_toml_hooks)
        assert reloaded.hooks.before_start == "echo global-before"
        assert "new-task" in reloaded.tasks


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


class TestAutoInjectAgentsRoundtrip:
    """auto_inject_agents must survive add/remove via the CLI write path.

    Regression: addTask/removeTask used to rebuild TaskmuxConfig without
    threading auto_inject_agents through, so the per-project toggle was
    silently lost on the next add/remove.
    """

    def test_false_survives_add(self, tmp_path: Path):
        cfg_path = tmp_path / "taskmux.toml"
        cfg_path.write_text(
            'name = "demo"\nauto_inject_agents = false\n\n[tasks.api]\ncommand = "x"\n'
        )
        addTask(cfg_path, "new", "echo new")
        reloaded = loadConfig(cfg_path)
        assert reloaded.auto_inject_agents is False
        assert "auto_inject_agents = false" in cfg_path.read_text()

    def test_true_survives_remove(self, tmp_path: Path):
        cfg_path = tmp_path / "taskmux.toml"
        cfg_path.write_text(
            'name = "demo"\nauto_inject_agents = true\n\n'
            '[tasks.a]\ncommand = "x"\n[tasks.b]\ncommand = "y"\n'
        )
        removeTask(cfg_path, "a")
        reloaded = loadConfig(cfg_path)
        assert reloaded.auto_inject_agents is True

    def test_unset_stays_unset(self, sample_toml: Path):
        """Default (None) should not pollute the file with an explicit value."""
        addTask(sample_toml, "new", "echo new")
        text = sample_toml.read_text()
        assert "auto_inject_agents" not in text
        assert loadConfig(sample_toml).auto_inject_agents is None
