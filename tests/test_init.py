"""Tests for project initialization."""

from pathlib import Path
from unittest.mock import patch

from taskmux.config import loadConfig
from taskmux.init import initProject


class TestInitProject:
    def test_defaults_creates_config(self, tmp_path: Path):
        cfg = initProject(path=tmp_path, defaults=True)
        assert (tmp_path / "taskmux.toml").exists()
        assert cfg.name == tmp_path.name

    def test_defaults_uses_dir_name(self, tmp_path: Path):
        cfg = initProject(path=tmp_path, defaults=True)
        assert cfg.name == tmp_path.name

    def test_config_is_loadable(self, tmp_path: Path):
        initProject(path=tmp_path, defaults=True)
        loaded = loadConfig(tmp_path / "taskmux.toml")
        assert loaded.name == tmp_path.name

    def test_aborts_if_exists(self, tmp_path: Path, capsys):
        (tmp_path / "taskmux.toml").write_text('name = "existing"\n')
        initProject(path=tmp_path, defaults=True)
        captured = capsys.readouterr()
        assert "already exists" in captured.out

    @patch("taskmux.init.detectInstalledAgents", return_value=["claude"])
    def test_injects_agents_with_defaults(self, mock_detect, tmp_path: Path):
        initProject(path=tmp_path, defaults=True)
        assert (tmp_path / ".claude" / "rules" / "taskmux.md").exists()

    @patch("taskmux.init.detectInstalledAgents", return_value=[])
    def test_no_agents_no_injection(self, mock_detect, tmp_path: Path):
        initProject(path=tmp_path, defaults=True)
        assert not (tmp_path / ".claude" / "rules" / "taskmux.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()

    @patch("taskmux.init.detectInstalledAgents", return_value=["claude"])
    @patch("builtins.input", side_effect=["my-session", "y"])
    def test_interactive_prompts(self, mock_input, mock_detect, tmp_path: Path):
        cfg = initProject(path=tmp_path, defaults=False)
        assert cfg.name == "my-session"
        assert (tmp_path / ".claude" / "rules" / "taskmux.md").exists()

    @patch("taskmux.init.detectInstalledAgents", return_value=["claude"])
    @patch("builtins.input", side_effect=["", "n"])
    def test_interactive_skip_agents(self, mock_input, mock_detect, tmp_path: Path):
        cfg = initProject(path=tmp_path, defaults=False)
        assert cfg.name == tmp_path.name
        assert not (tmp_path / ".claude" / "rules" / "taskmux.md").exists()
