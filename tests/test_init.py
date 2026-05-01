"""Tests for project initialization."""

from pathlib import Path
from unittest.mock import patch

from taskmux.agent import AGENTS_FILE, CLAUDE_FILE, CONTEXT_START
from taskmux.config import loadConfig
from taskmux.init import initProject
from taskmux.models import slugify


def _silenceSkill(monkeypatch):
    """Skip skill-installed lookup so tests don't depend on the host's home."""
    monkeypatch.setattr("taskmux.init.skillInstalled", lambda _p=None: True)


class TestInitProject:
    def test_defaults_creates_config(self, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        cfg = initProject(path=tmp_path, defaults=True)
        assert (tmp_path / "taskmux.toml").exists()
        assert cfg.name == slugify(tmp_path.name)

    def test_defaults_uses_dir_name(self, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        cfg = initProject(path=tmp_path, defaults=True)
        assert cfg.name == slugify(tmp_path.name)

    def test_config_is_loadable(self, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        initProject(path=tmp_path, defaults=True)
        loaded = loadConfig(tmp_path / "taskmux.toml")
        assert loaded.name == slugify(tmp_path.name)

    def test_aborts_if_exists(self, tmp_path: Path, capsys, monkeypatch):
        _silenceSkill(monkeypatch)
        (tmp_path / "taskmux.toml").write_text('name = "existing"\n')
        initProject(path=tmp_path, defaults=True)
        captured = capsys.readouterr()
        assert "already exists" in captured.out

    def test_defaults_creates_agents_md_when_neither_exists(self, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        initProject(path=tmp_path, defaults=True)
        assert (tmp_path / AGENTS_FILE).exists()
        content = (tmp_path / AGENTS_FILE).read_text()
        assert CONTEXT_START in content
        assert not (tmp_path / CLAUDE_FILE).exists()

    def test_patches_existing_claude_md(self, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        (tmp_path / CLAUDE_FILE).write_text("# project rules\n")
        initProject(path=tmp_path, defaults=True)
        content = (tmp_path / CLAUDE_FILE).read_text()
        assert "# project rules" in content
        assert CONTEXT_START in content
        assert not (tmp_path / AGENTS_FILE).exists()

    def test_patches_both_when_both_exist(self, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        (tmp_path / CLAUDE_FILE).write_text("# claude\n")
        (tmp_path / AGENTS_FILE).write_text("# agents\n")
        initProject(path=tmp_path, defaults=True)
        assert CONTEXT_START in (tmp_path / CLAUDE_FILE).read_text()
        assert CONTEXT_START in (tmp_path / AGENTS_FILE).read_text()

    @patch("builtins.input", side_effect=["my-session", ""])
    def test_interactive_default_picks_agents_md(self, mock_input, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        cfg = initProject(path=tmp_path, defaults=False)
        assert cfg.name == "my-session"
        assert (tmp_path / AGENTS_FILE).exists()
        assert not (tmp_path / CLAUDE_FILE).exists()

    @patch("builtins.input", side_effect=["", "2"])
    def test_interactive_picks_claude_md(self, mock_input, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        initProject(path=tmp_path, defaults=False)
        assert (tmp_path / CLAUDE_FILE).exists()
        assert not (tmp_path / AGENTS_FILE).exists()

    @patch("builtins.input", side_effect=["", "3"])
    def test_interactive_picks_both(self, mock_input, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        initProject(path=tmp_path, defaults=False)
        assert (tmp_path / CLAUDE_FILE).exists()
        assert (tmp_path / AGENTS_FILE).exists()

    @patch("builtins.input", side_effect=["", "s"])
    def test_interactive_skip(self, mock_input, tmp_path: Path, monkeypatch):
        _silenceSkill(monkeypatch)
        initProject(path=tmp_path, defaults=False)
        assert not (tmp_path / CLAUDE_FILE).exists()
        assert not (tmp_path / AGENTS_FILE).exists()

    def test_skill_tip_shown_when_missing(self, tmp_path: Path, monkeypatch, capsys):
        monkeypatch.setattr("taskmux.init.skillInstalled", lambda _p=None: False)
        initProject(path=tmp_path, defaults=True)
        captured = capsys.readouterr()
        assert "npx skills add nc9/taskmux" in captured.out

    def test_skill_tip_suppressed_when_installed(self, tmp_path: Path, monkeypatch, capsys):
        _silenceSkill(monkeypatch)
        initProject(path=tmp_path, defaults=True)
        captured = capsys.readouterr()
        assert "npx skills add" not in captured.out
