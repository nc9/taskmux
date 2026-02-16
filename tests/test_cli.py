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


class TestInitCommand:
    @patch("taskmux.cli.initProject")
    def test_init_defaults(self, mock_init):
        result = runner.invoke(app, ["init", "--defaults"])
        assert result.exit_code == 0
        mock_init.assert_called_once_with(defaults=True)

    @patch("taskmux.cli.initProject")
    def test_init_interactive(self, mock_init):
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        mock_init.assert_called_once_with(defaults=False)


class TestStartCommand:
    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_start_all(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["start"])
        assert result.exit_code == 0
        mock_tmux.return_value.start_all.assert_called_once()

    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_start_task(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["start", "server"])
        assert result.exit_code == 0
        mock_tmux.return_value.start_task.assert_called_once_with("server")


class TestStopCommand:
    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_stop_all(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        mock_tmux.return_value.stop_all.assert_called_once()

    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_stop_task(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["stop", "server"])
        assert result.exit_code == 0
        mock_tmux.return_value.stop_task.assert_called_once_with("server")


class TestRestartCommand:
    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_restart_all(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["restart"])
        assert result.exit_code == 0
        mock_tmux.return_value.restart_all.assert_called_once()

    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_restart_task(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["restart", "server"])
        assert result.exit_code == 0
        mock_tmux.return_value.restart_task.assert_called_once_with("server")


class TestInspectCommand:
    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_inspect_calls_method(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        mock_tmux.return_value.inspect_task.return_value = {
            "name": "server",
            "running": False,
        }
        result = runner.invoke(app, ["inspect", "server"])
        assert result.exit_code == 0
        mock_tmux.return_value.inspect_task.assert_called_once_with("server")


class TestLogsCommand:
    @patch("taskmux.cli.TmuxManager")
    @patch("taskmux.cli.loadConfig")
    def test_logs_with_grep(self, mock_load, mock_tmux):
        from taskmux.models import TaskmuxConfig

        mock_load.return_value = TaskmuxConfig()
        result = runner.invoke(app, ["logs", "server", "--grep", "error", "-C", "2"])
        assert result.exit_code == 0
        mock_tmux.return_value.show_logs.assert_called_once_with(
            "server", False, 100, grep="error", context=2
        )


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
