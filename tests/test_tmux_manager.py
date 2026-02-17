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


class TestToposortTasks:
    def test_no_deps(self):
        cfg = _make_config(tasks={"a": "echo a", "b": "echo b"})
        mgr = _make_manager(cfg)
        result = mgr._toposort_tasks(["a", "b"])
        assert set(result) == {"a", "b"}

    def test_linear_deps(self):
        cfg = _make_config(
            tasks={
                "db": "echo db",
                "api": {"command": "echo api", "depends_on": ["db"]},
                "web": {"command": "echo web", "depends_on": ["api"]},
            }
        )
        mgr = _make_manager(cfg)
        result = mgr._toposort_tasks(["db", "api", "web"])
        assert result.index("db") < result.index("api") < result.index("web")

    def test_diamond_deps(self):
        cfg = _make_config(
            tasks={
                "db": "echo db",
                "cache": "echo cache",
                "api": {"command": "echo api", "depends_on": ["db", "cache"]},
            }
        )
        mgr = _make_manager(cfg)
        result = mgr._toposort_tasks(["db", "cache", "api"])
        assert result.index("db") < result.index("api")
        assert result.index("cache") < result.index("api")


class TestIsTaskHealthy:
    @patch("taskmux.tmux_manager.subprocess.run")
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_health_check_command_success(self, mock_server_cls, mock_run):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")
        mock_run.return_value = MagicMock(returncode=0)

        cfg = _make_config(tasks={"api": {"command": "echo api", "health_check": "curl localhost"}})
        mgr = TmuxManager(cfg)
        assert mgr.is_task_healthy("api") is True
        mock_run.assert_called_once()

    @patch("taskmux.tmux_manager.subprocess.run")
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_health_check_command_failure(self, mock_server_cls, mock_run):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")
        mock_run.return_value = MagicMock(returncode=1)

        cfg = _make_config(tasks={"api": {"command": "echo api", "health_check": "curl localhost"}})
        mgr = TmuxManager(cfg)
        assert mgr.is_task_healthy("api") is False

    @patch("taskmux.tmux_manager.subprocess.run")
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_health_check_timeout(self, mock_server_cls, mock_run):
        import subprocess

        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")
        mock_run.side_effect = subprocess.TimeoutExpired("cmd", 5)

        cfg = _make_config(tasks={"api": {"command": "echo api", "health_check": "curl localhost"}})
        mgr = TmuxManager(cfg)
        assert mgr.is_task_healthy("api") is False

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_fallback_to_pane_alive(self, mock_server_cls):
        """No health_check configured -> falls back to _is_pane_alive."""
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        mock_window = MagicMock()
        mock_pane = MagicMock()
        mock_pane.pane_current_command = "node"
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        assert mgr.is_task_healthy("server") is True

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_unknown_task(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")

        cfg = _make_config(tasks={"server": "echo hi"})
        mgr = TmuxManager(cfg)
        assert mgr.is_task_healthy("ghost") is False


class TestCwd:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_send_command_passes_start_directory(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        mock_window = MagicMock()
        mock_window.active_pane = MagicMock()
        mock_session.new_window.return_value = mock_window

        cfg = _make_config(tasks={"api": {"command": "cargo run", "cwd": "apps/api"}})
        mgr = TmuxManager(cfg)
        mgr._send_command_to_window(mock_session, "api", "cargo run", "apps/api")
        mock_session.new_window.assert_called_once_with(
            attach=False, window_name="api", start_directory="apps/api"
        )

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_send_command_no_cwd(self, mock_server_cls):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.active_pane = MagicMock()
        mock_session.new_window.return_value = mock_window

        cfg = _make_config(tasks={"api": "cargo run"})
        mgr = TmuxManager(cfg)
        mgr._send_command_to_window(mock_session, "api", "cargo run")
        mock_session.new_window.assert_called_once_with(attach=False, window_name="api")


class TestShowAllLogs:
    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_prefixed_output(self, mock_server_cls, capsys):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        # Create two windows with output
        mock_pane_a = MagicMock()
        mock_pane_a.cmd.return_value = MagicMock(stdout=["line1", "line2"])
        mock_window_a = MagicMock()
        mock_window_a.active_pane = mock_pane_a

        mock_pane_b = MagicMock()
        mock_pane_b.cmd.return_value = MagicMock(stdout=["lineX"])
        mock_window_b = MagicMock()
        mock_window_b.active_pane = mock_pane_b

        def get_window(window_name, default=None):
            return {"srv": mock_window_a, "web": mock_window_b}.get(window_name, default)

        mock_session.windows.get = get_window

        cfg = _make_config(tasks={"srv": "echo srv", "web": "echo web"})
        mgr = TmuxManager(cfg)
        mgr.show_all_logs(lines=50)

        captured = capsys.readouterr().out
        assert "[srv] line1" in captured
        assert "[srv] line2" in captured
        assert "[web] lineX" in captured

    @patch("taskmux.tmux_manager.libtmux.Server")
    def test_grep_filter(self, mock_server_cls, capsys):
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        mock_session = MagicMock()
        mock_server.sessions.get.return_value = mock_session

        mock_pane = MagicMock()
        mock_pane.cmd.return_value = MagicMock(stdout=["info ok", "ERROR bad", "info fine"])
        mock_window = MagicMock()
        mock_window.active_pane = mock_pane
        mock_session.windows.get.return_value = mock_window

        cfg = _make_config(tasks={"srv": "echo srv"})
        mgr = TmuxManager(cfg)
        mgr.show_all_logs(grep="error")

        captured = capsys.readouterr().out
        assert "[srv] ERROR bad" in captured
        assert "info ok" not in captured


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
