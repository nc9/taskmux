"""Tests for TmuxManager (mock libtmux.Server to avoid tmux dependency)."""

from unittest.mock import MagicMock, patch

from taskmux.models import TaskConfig, TaskmuxConfig
from taskmux.tmux_manager import TmuxManager


def _make_config(**tasks) -> TaskmuxConfig:
    return TaskmuxConfig(
        name="test",
        tasks={
            k: TaskConfig(**v) if isinstance(v, dict) else TaskConfig(command=v)
            for k, v in tasks.items()
        },
    )


class TestCreateSession:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_skips_auto_start_false(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        mock_session = MagicMock()
        mock_server.new_session.return_value = mock_session
        mock_window = MagicMock()
        mock_session.windows = [mock_window]
        mock_window.active_pane = MagicMock()

        cfg = _make_config(
            server="echo server",
            watcher={"command": "cargo watch", "auto_start": False},
        )
        mgr = TmuxManager(cfg)
        mgr.create_session()

        # Only 1 auto_start task, so new_window should not be called for watcher
        mock_session.new_window.assert_not_called()
        mock_window.rename_window.assert_called_once_with("server")


class TestGetTaskStatus:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_returns_command(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        cfg = _make_config(server="echo hi")
        mgr = TmuxManager(cfg)
        status = mgr.get_task_status("server")
        assert status["command"] == "echo hi"
        assert status["running"] is False


class TestRestartTask:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_unknown_task_doesnt_crash(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.return_value = MagicMock()

        cfg = _make_config(server="echo hi")
        mgr = TmuxManager(cfg)
        mgr.restart_task("nonexistent")  # should print error, not crash
