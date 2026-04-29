"""Tests for agent context-file detection and injection."""

from pathlib import Path
from unittest.mock import patch

from taskmux.agent import (
    AGENTS_FILE,
    CLAUDE_FILE,
    CONTEXT_END,
    CONTEXT_START,
    buildContextBlock,
    detectContextFiles,
    detectInstalledAgents,
    injectIntoFile,
    reinjectIfEnabled,
    skillInstalled,
)
from taskmux.models import TaskConfig, TaskmuxConfig


class TestDetectInstalledAgents:
    @patch("taskmux.agent.shutil.which")
    def test_detects_claude(self, mock_which):
        mock_which.side_effect = lambda b: "/usr/bin/claude" if b == "claude" else None
        agents = detectInstalledAgents()
        assert "claude" in agents
        assert "codex" not in agents

    @patch("taskmux.agent.shutil.which", return_value=None)
    def test_none_installed(self, mock_which):
        assert detectInstalledAgents() == []

    @patch("taskmux.agent.shutil.which", return_value="/usr/bin/x")
    def test_all_installed(self, mock_which):
        agents = detectInstalledAgents()
        assert {"claude", "codex", "opencode"} <= set(agents)


class TestDetectContextFiles:
    def test_none_when_empty(self, tmp_path: Path):
        assert detectContextFiles(tmp_path) == []

    def test_finds_claude_md(self, tmp_path: Path):
        (tmp_path / CLAUDE_FILE).write_text("hi\n")
        assert detectContextFiles(tmp_path) == [tmp_path / CLAUDE_FILE]

    def test_finds_agents_md(self, tmp_path: Path):
        (tmp_path / AGENTS_FILE).write_text("hi\n")
        assert detectContextFiles(tmp_path) == [tmp_path / AGENTS_FILE]

    def test_finds_both(self, tmp_path: Path):
        (tmp_path / CLAUDE_FILE).write_text("hi\n")
        (tmp_path / AGENTS_FILE).write_text("hi\n")
        names = sorted(p.name for p in detectContextFiles(tmp_path))
        assert names == sorted([CLAUDE_FILE, AGENTS_FILE])


class TestSkillInstalled:
    def test_finds_user_claude_skill(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        (fake_home / ".claude" / "skills" / "taskmux").mkdir(parents=True)
        (fake_home / ".claude" / "skills" / "taskmux" / "SKILL.md").write_text("x")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert skillInstalled() is True

    def test_finds_user_agents_skill(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        (fake_home / ".agents" / "skills" / "taskmux").mkdir(parents=True)
        (fake_home / ".agents" / "skills" / "taskmux" / "SKILL.md").write_text("x")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert skillInstalled() is True

    def test_finds_project_claude_skill(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (tmp_path / ".claude" / "skills" / "taskmux").mkdir(parents=True)
        (tmp_path / ".claude" / "skills" / "taskmux" / "SKILL.md").write_text("x")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert skillInstalled(tmp_path) is True

    def test_finds_project_agents_skill(self, tmp_path: Path, monkeypatch):
        """`.agents/skills/` is the shared cross-agent project-local convention."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (tmp_path / ".agents" / "skills" / "taskmux").mkdir(parents=True)
        (tmp_path / ".agents" / "skills" / "taskmux" / "SKILL.md").write_text("x")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert skillInstalled(tmp_path) is True

    def test_finds_global_opencode_skill(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        (fake_home / ".config" / "opencode" / "skills" / "taskmux").mkdir(parents=True)
        (fake_home / ".config" / "opencode" / "skills" / "taskmux" / "SKILL.md").write_text("x")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert skillInstalled() is True

    def test_missing(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert skillInstalled(tmp_path) is False


class TestBuildContextBlock:
    def test_contains_markers(self):
        block = buildContextBlock(TaskmuxConfig(name="test"))
        assert CONTEXT_START in block
        assert CONTEXT_END in block

    def test_contains_project_name(self):
        block = buildContextBlock(TaskmuxConfig(name="my-project"))
        assert "my-project" in block

    def test_lists_tasks(self):
        cfg = TaskmuxConfig(
            name="test",
            tasks={
                "server": TaskConfig(command="npm start"),
                "worker": TaskConfig(command="celery worker", auto_start=False),
            },
        )
        block = buildContextBlock(cfg)
        assert "server" in block
        assert "npm start" in block
        assert "| no |" in block

    def test_table_includes_url(self):
        cfg = TaskmuxConfig(name="test", tasks={"api": TaskConfig(command="bun dev", host="api")})
        block = buildContextBlock(cfg)
        assert "https://api.test.localhost" in block

    def test_empty_tasks_message(self):
        block = buildContextBlock(TaskmuxConfig(name="test"))
        assert "No tasks configured yet" in block

    def test_contains_skill_pointer(self):
        block = buildContextBlock(TaskmuxConfig(name="test"))
        assert "taskmux` skill" in block
        assert "taskmux --help" in block
        assert "taskmux inspect" in block


class TestInjectIntoFile:
    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / AGENTS_FILE
        injectIntoFile(target, TaskmuxConfig(name="test"))
        assert target.exists()
        content = target.read_text()
        assert CONTEXT_START in content
        assert "test" in content

    def test_replaces_existing_block(self, tmp_path: Path):
        target = tmp_path / AGENTS_FILE
        injectIntoFile(target, TaskmuxConfig(name="v1"))
        injectIntoFile(target, TaskmuxConfig(name="v2"))
        content = target.read_text()
        assert "v2" in content
        assert "v1" not in content
        assert content.count(CONTEXT_START) == 1

    def test_appends_if_no_existing_block(self, tmp_path: Path):
        target = tmp_path / CLAUDE_FILE
        target.write_text("# Existing content\n")
        injectIntoFile(target, TaskmuxConfig(name="test"))
        content = target.read_text()
        assert "# Existing content" in content
        assert CONTEXT_START in content

    def test_writes_to_claude_md(self, tmp_path: Path):
        target = tmp_path / CLAUDE_FILE
        injectIntoFile(target, TaskmuxConfig(name="test"))
        assert target.name == CLAUDE_FILE

    def test_writes_to_agents_md(self, tmp_path: Path):
        target = tmp_path / AGENTS_FILE
        injectIntoFile(target, TaskmuxConfig(name="test"))
        assert target.name == AGENTS_FILE


class TestReinjectIfEnabled:
    def test_rewrites_existing_files(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "taskmux.global_config.loadGlobalConfig",
            lambda: type("GC", (), {"auto_inject_agents": True})(),
        )
        (tmp_path / AGENTS_FILE).write_text("# notes\n")
        injectIntoFile(tmp_path / AGENTS_FILE, TaskmuxConfig(name="orig"))

        rewrote = reinjectIfEnabled(
            tmp_path,
            TaskmuxConfig(name="orig", tasks={"new": TaskConfig(command="bun dev")}),
        )
        assert rewrote == [tmp_path / AGENTS_FILE]
        content = (tmp_path / AGENTS_FILE).read_text()
        assert "bun dev" in content
        assert "| new |" in content

    def test_no_op_when_no_files_exist(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "taskmux.global_config.loadGlobalConfig",
            lambda: type("GC", (), {"auto_inject_agents": True})(),
        )
        rewrote = reinjectIfEnabled(tmp_path, TaskmuxConfig(name="x"))
        assert rewrote == []

    def test_disabled_per_project(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "taskmux.global_config.loadGlobalConfig",
            lambda: type("GC", (), {"auto_inject_agents": True})(),
        )
        (tmp_path / AGENTS_FILE).write_text("# notes\n")
        injectIntoFile(tmp_path / AGENTS_FILE, TaskmuxConfig(name="orig"))
        before = (tmp_path / AGENTS_FILE).read_text()

        rewrote = reinjectIfEnabled(
            tmp_path,
            TaskmuxConfig(name="orig", auto_inject_agents=False),
        )
        assert rewrote == []
        assert (tmp_path / AGENTS_FILE).read_text() == before

    def test_disabled_globally(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(
            "taskmux.global_config.loadGlobalConfig",
            lambda: type("GC", (), {"auto_inject_agents": False})(),
        )
        (tmp_path / AGENTS_FILE).write_text("# notes\n")
        injectIntoFile(tmp_path / AGENTS_FILE, TaskmuxConfig(name="orig"))
        before = (tmp_path / AGENTS_FILE).read_text()

        rewrote = reinjectIfEnabled(tmp_path, TaskmuxConfig(name="orig"))
        assert rewrote == []
        assert (tmp_path / AGENTS_FILE).read_text() == before

    def test_per_project_overrides_global_disabled(self, tmp_path: Path, monkeypatch):
        """auto_inject_agents=True in taskmux.toml beats global False."""
        monkeypatch.setattr(
            "taskmux.global_config.loadGlobalConfig",
            lambda: type("GC", (), {"auto_inject_agents": False})(),
        )
        (tmp_path / AGENTS_FILE).write_text("# notes\n")
        rewrote = reinjectIfEnabled(
            tmp_path,
            TaskmuxConfig(name="orig", auto_inject_agents=True),
        )
        assert rewrote == [tmp_path / AGENTS_FILE]
