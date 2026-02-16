"""Tests for agent detection and context injection."""

from pathlib import Path
from unittest.mock import patch

from taskmux.agent import (
    CONTEXT_END,
    CONTEXT_START,
    buildContextBlock,
    detectInstalledAgents,
    injectAgentContext,
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
        agents = detectInstalledAgents()
        assert agents == []

    @patch("taskmux.agent.shutil.which", return_value="/usr/bin/x")
    def test_all_installed(self, mock_which):
        agents = detectInstalledAgents()
        assert "claude" in agents
        assert "codex" in agents
        assert "opencode" in agents


class TestBuildContextBlock:
    def test_contains_markers(self):
        cfg = TaskmuxConfig(name="test")
        block = buildContextBlock(cfg)
        assert CONTEXT_START in block
        assert CONTEXT_END in block

    def test_contains_project_name(self):
        cfg = TaskmuxConfig(name="my-project")
        block = buildContextBlock(cfg)
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
        assert "(manual)" in block

    def test_empty_tasks_message(self):
        cfg = TaskmuxConfig(name="test")
        block = buildContextBlock(cfg)
        assert "No tasks configured yet" in block

    def test_contains_usage(self):
        cfg = TaskmuxConfig(name="test")
        block = buildContextBlock(cfg)
        assert "taskmux start" in block
        assert "taskmux inspect" in block


class TestInjectAgentContext:
    def test_creates_file(self, tmp_path: Path):
        cfg = TaskmuxConfig(name="test")
        result = injectAgentContext("claude", tmp_path, cfg)
        assert result.exists()
        content = result.read_text()
        assert CONTEXT_START in content
        assert "test" in content

    def test_creates_parent_dirs(self, tmp_path: Path):
        cfg = TaskmuxConfig(name="test")
        result = injectAgentContext("claude", tmp_path, cfg)
        assert result.parent.exists()
        assert ".claude/rules" in str(result)

    def test_replaces_existing_block(self, tmp_path: Path):
        cfg = TaskmuxConfig(name="v1")
        injectAgentContext("claude", tmp_path, cfg)

        cfg2 = TaskmuxConfig(name="v2")
        result = injectAgentContext("claude", tmp_path, cfg2)
        content = result.read_text()
        assert "v2" in content
        assert "v1" not in content
        assert content.count(CONTEXT_START) == 1

    def test_appends_if_no_existing_block(self, tmp_path: Path):
        target = tmp_path / ".claude" / "rules" / "taskmux.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Existing content\n")

        cfg = TaskmuxConfig(name="test")
        injectAgentContext("claude", tmp_path, cfg)
        content = target.read_text()
        assert "# Existing content" in content
        assert CONTEXT_START in content

    def test_codex_uses_agents_md(self, tmp_path: Path):
        cfg = TaskmuxConfig(name="test")
        result = injectAgentContext("codex", tmp_path, cfg)
        assert result.name == "AGENTS.md"
