"""Tests for HTTP/TCP/shell probes and HealthResult plumbing."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest

from taskmux.models import TaskConfig, TaskmuxConfig
from taskmux.tmux_manager import HealthResult, TmuxManager


def _make_config(**kwargs) -> TaskmuxConfig:
    tasks = kwargs.pop("tasks", {})
    parsed = {
        k: TaskConfig(**v) if isinstance(v, dict) else TaskConfig(command=v)
        for k, v in tasks.items()
    }
    return TaskmuxConfig(tasks=parsed, **kwargs)


def _make_manager(config: TaskmuxConfig) -> TmuxManager:
    with patch("taskmux.tmux_manager.libtmux.Server") as mock_cls:
        mock_server = MagicMock()
        mock_cls.return_value = mock_server
        mock_server.sessions.get.side_effect = Exception("not found")
        return TmuxManager(config)


class _SilentHandler(BaseHTTPRequestHandler):
    body = b'<html><body><div id="__next">app</div></body></html>'
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def http_server():
    """Spin up an HTTP server on an ephemeral port; yield (port, handler_cls)."""
    handler = type("H", (_SilentHandler,), {})
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, handler
    finally:
        server.shutdown()
        server.server_close()


class TestHttpProbe:
    def test_status_match_no_body_check(self, http_server):
        port, _ = http_server
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_http(f"http://127.0.0.1:{port}/", 2.0, 200, None)
        assert result.ok is True
        assert result.method == "http"
        assert result.reason is None

    def test_status_mismatch(self, http_server):
        port, handler = http_server
        handler.status = 500
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_http(f"http://127.0.0.1:{port}/", 2.0, 200, None)
        assert result.ok is False
        assert "500" in (result.reason or "")

    def test_body_match_pass(self, http_server):
        port, _ = http_server
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_http(f"http://127.0.0.1:{port}/", 2.0, 200, r'id="__next"')
        assert result.ok is True

    def test_body_mismatch(self, http_server):
        port, _ = http_server
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_http(f"http://127.0.0.1:{port}/", 2.0, 200, r"NEVERMATCH")
        assert result.ok is False
        assert "body mismatch" in (result.reason or "")

    def test_connect_refused(self):
        # Find a free port, close it, then probe — should fail fast
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_http(f"http://127.0.0.1:{port}/", 1.0, 200, None)
        assert result.ok is False
        assert result.method == "http"


class TestTcpProbe:
    def test_open_port(self, http_server):
        port, _ = http_server
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_tcp(port, 1.0)
        assert result.ok is True
        assert result.method == "tcp"

    def test_closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr._probe_tcp(port, 1.0)
        assert result.ok is False
        assert "refused" in (result.reason or "")


class TestPrecedence:
    """check_health() picks the right probe based on config."""

    def test_url_beats_shell(self, http_server):
        port, _ = http_server
        cfg = _make_config(
            tasks={
                "web": {
                    "command": "echo",
                    "health_url": f"http://127.0.0.1:{port}/",
                    "health_check": "false",  # would fail if used
                }
            }
        )
        mgr = _make_manager(cfg)
        result = mgr.check_health("web")
        assert result.ok is True
        assert result.method == "http"

    def test_shell_beats_tcp(self):
        cfg = _make_config(
            tasks={
                "web": {
                    "command": "echo",
                    "health_check": "true",
                    "port": 1,  # closed
                }
            }
        )
        mgr = _make_manager(cfg)
        result = mgr.check_health("web")
        assert result.ok is True
        assert result.method == "shell"

    def test_tcp_used_when_only_port(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "port": port}})
        mgr = _make_manager(cfg)
        result = mgr.check_health("web")
        assert result.ok is True
        assert result.method == "tcp"

    def test_pane_fallback_when_no_config(self):
        cfg = _make_config(tasks={"web": "echo"})
        mgr = _make_manager(cfg)
        with patch.object(mgr, "_is_pane_alive", return_value=True):
            result = mgr.check_health("web")
        assert result.method == "pane"
        assert result.ok is True

    def test_unknown_task(self):
        mgr = _make_manager(_make_config(tasks={"web": "echo"}))
        result = mgr.check_health("ghost")
        assert result.ok is False
        assert result.method == "none"


class TestLastHealthRecorded:
    def test_check_health_stores_on_tracker(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "port": port}})
        mgr = _make_manager(cfg)
        assert mgr.restart_tracker.last_health("web") is None
        mgr.check_health("web")
        last = mgr.restart_tracker.last_health("web")
        assert last is not None
        assert isinstance(last, HealthResult)
        assert last.method == "tcp"
        assert last.ok is True

    def test_list_tasks_surfaces_last_health(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "port": port}})
        mgr = _make_manager(cfg)
        mgr.check_health("web")
        data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["last_health"] is not None
        assert task["last_health"]["method"] == "tcp"
        assert task["last_health"]["ok"] is True


class TestIsHealthyBackcompat:
    """is_task_healthy still returns bool — existing callers unaffected."""

    def test_returns_bool(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "port": port}})
        mgr = _make_manager(cfg)
        assert mgr.is_task_healthy("web") is True
