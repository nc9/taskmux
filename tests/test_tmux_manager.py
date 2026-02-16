"""Tests for TmuxManager (mock libtmux.Server to avoid tmux dependency)."""

from unittest.mock import MagicMock, patch

from taskmux.models import TaskConfig, TaskmuxConfig
from taskmux.tmux_manager import TmuxManager, _print_grep_results


def _make_config(**kwargs) -> TaskmuxConfig:
    tasks = kwargs.pop("tasks", {})
    parsed_tasks = {
        k: TaskConfig(**v) if isinstance(v, dict) else TaskConfig(command=v)
        for k, v in tasks.items()
    }
    return TaskmuxConfig(tasks=parsed_tasks, **kwargs)


def _make_manager(config: TaskmuxConfig) -> TmuxManager:
    with patch("taskmux.tmux_manager.libtmux.Server") as mock_cls:
        mock_server = MagicMock()
        mock_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")
        return TmuxManager(config)


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
            tasks={
                "server": "echo server",
                "watcher": {"command": "cargo watch", "auto_start": False},
            },
        )
        mgr = TmuxManager(cfg)
        mgr.create_session()

        # Only 1 auto_start task, so new_window should not be called for watcher
        mock_session.new_window.assert_not_called()
        mock_window.rename_window.assert_called_once_with("server")


class TestStartTask:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_unknown_task(self, mock_server_cls, capsys):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.start_task("ghost")
        assert "not found" in capsys.readouterr().out

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_creates_session_if_missing(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        mock_session = MagicMock()
        mock_server.new_session.return_value = mock_session
        mock_window = MagicMock()
        mock_window.window_name = "bash"
        # windows must support both len/index and .get()
        mock_windows = MagicMock()
        mock_windows.__len__ = lambda s: 1
        mock_windows.__getitem__ = lambda s, i: mock_window
        mock_windows.get.return_value = None
        mock_session.windows = mock_windows
        mock_window.active_pane = MagicMock()

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.start_task("server")
        mock_server.new_session.assert_called_once()


class TestStopTask:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_sends_ctrl_c(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.stop_task("server")
        mock_pane.send_keys.assert_called_with("C-c")

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_not_running(self, mock_server_cls, capsys):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = None

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.stop_task("server")
        assert "not running" in capsys.readouterr().out


class TestStopAll:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_kills_session(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        mock_session.windows.get.return_value = None

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.stop_all()
        mock_session.kill.assert_called_once()


class TestStartAll:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_global_auto_start_false(self, mock_server_cls, capsys):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")
        mock_session = MagicMock()
        mock_server.new_session.return_value = mock_session

        cfg = _make_config(auto_start=False, tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.start_all()
        output = capsys.readouterr().out
        assert "auto_start disabled" in output
        mock_server.new_session.assert_called_once()


class TestInspectTask:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_unknown_task(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        result = mgr.inspect_task("ghost")
        assert "error" in result

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_no_session(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        result = mgr.inspect_task("server")
        assert result["name"] == "server"
        assert result["running"] is False
        assert result["command"] == "echo hi"

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_running_task(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        mock_window = MagicMock()
        mock_window.window_id = "@1"
        mock_pane = MagicMock()
        mock_pane.pane_id = "%1"
        mock_pane.pane_pid = "12345"
        mock_pane.pane_current_command = "node"
        mock_pane.pane_current_path = "/tmp"
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        result = mgr.inspect_task("server")
        assert result["running"] is True
        assert result["healthy"] is True
        assert result["pid"] == "12345"
        assert result["pane_current_command"] == "node"


class TestGetTaskStatus:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_returns_command(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        cfg = _make_config(tasks={"server": "echo hi"})
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

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        mgr.restart_task("nonexistent")  # should print error, not crash


class TestPrintGrepResults:
    def test_matching_lines(self, capsys):
        output = ["line 1", "error here", "line 3", "another error", "line 5"]
        _print_grep_results(output, "error", context=0)
        captured = capsys.readouterr().out
        assert "error here" in captured
        assert "another error" in captured

    def test_no_matches(self, capsys):
        output = ["line 1", "line 2"]
        _print_grep_results(output, "missing", context=0)
        captured = capsys.readouterr().out
        assert "No matches" in captured

    def test_context_lines(self, capsys):
        output = ["a", "b", "MATCH", "d", "e"]
        _print_grep_results(output, "MATCH", context=1)
        captured = capsys.readouterr().out
        assert "b" in captured
        assert "MATCH" in captured
        assert "d" in captured

    def test_separator_between_groups(self, capsys):
        output = ["a", "MATCH1", "b", "c", "d", "e", "MATCH2", "f"]
        _print_grep_results(output, "MATCH", context=0)
        captured = capsys.readouterr().out
        assert "--" in captured
