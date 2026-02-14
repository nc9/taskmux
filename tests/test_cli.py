"""Tests for CLI commands (mock TmuxManager to avoid tmux dependency)."""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from taskmux.cli import app
from taskmux.config import loadConfig

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "taskmux" in result.output.lower()


class TestAddCommand:
    def test_add_creates_task(self, sample_toml: Path):
        with (
            patch("taskmux.cli.loadConfig", side_effect=lambda: loadConfig(sample_toml)),
            patch("taskmux.cli.addTask") as mock_add,
        ):
            result = runner.invoke(app, ["add", "web", "npm start"])
            assert result.exit_code == 0
            mock_add.assert_called_once_with(None, "web", "npm start")


class TestRemoveCommand:
    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_remove_calls_removeTask(self, mock_load, mock_tmux, sample_toml: Path):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        mock_tmux.return_value.session_exists.return_value = False

        with patch("taskmux.cli.removeTask", return_value=(TaskmuxConfig(), True)) as mock_rm:
            result = runner.invoke(app, ["remove", "server"])
            assert result.exit_code == 0
            mock_rm.assert_called_once_with(None, "server")
