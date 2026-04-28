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
                    "host": "web",
                }
            }
        )
        mgr = _make_manager(cfg)
        mgr.assigned_ports["web"] = 1  # closed
        result = mgr.check_health("web")
        assert result.ok is True
        assert result.method == "shell"

    def test_tcp_used_when_only_host(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = _make_manager(cfg)
        mgr.assigned_ports["web"] = port
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
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = _make_manager(cfg)
        mgr.assigned_ports["web"] = port
        assert mgr.restart_tracker.last_health("web") is None
        mgr.check_health("web")
        last = mgr.restart_tracker.last_health("web")
        assert last is not None
        assert isinstance(last, HealthResult)
        assert last.method == "tcp"
        assert last.ok is True

    def test_list_tasks_surfaces_last_health(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = _make_manager(cfg)
        mgr.assigned_ports["web"] = port
        mgr.check_health("web")
        with patch.object(
            mgr, "_proxy_listener_status", return_value={"bound": True, "port": 443, "reason": None}
        ):
            data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["last_health"] is not None
        assert task["last_health"]["method"] == "tcp"
        assert task["last_health"]["ok"] is True


# Tests use a non-privileged port so they never depend on :443 being free
# or root being available. Production default stays at 443.
TEST_PROXY_PORT = 18443


class TestProxyListenerEnrichment:
    """list_tasks() flips healthy=false when a host-routed task's proxy isn't bound."""

    def _running_manager(self, cfg: TaskmuxConfig) -> TmuxManager:
        """Return a manager that pretends the session + task pane are alive,
        so list_tasks() observes status['healthy']=True from the upstream
        probe and the proxy-override branch is reachable.
        """
        mgr = _make_manager(cfg)
        # Stub out session/window/pane checks — list_tasks uses these to
        # decide running/healthy before we ever get to the proxy override.
        mgr.session_exists = lambda: True  # type: ignore[method-assign]
        mgr.list_windows = lambda: list(cfg.tasks.keys())  # type: ignore[method-assign]
        mgr._is_pane_alive = lambda _name: True  # type: ignore[method-assign]
        return mgr

    def test_proxy_unbound_overrides_healthy(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = self._running_manager(cfg)
        mgr.assigned_ports["web"] = port
        with patch.object(
            mgr,
            "_proxy_listener_status",
            return_value={
                "bound": False,
                "port": TEST_PROXY_PORT,
                "reason": (
                    f"proxy listener not bound on 127.0.0.1:{TEST_PROXY_PORT} — "
                    "run `taskmux daemon` to start it"
                ),
                "routes": None,
            },
        ):
            data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["healthy"] is False
        assert task["last_health"]["method"] == "proxy"
        assert "not bound" in task["last_health"]["reason"]
        assert data["proxy"]["bound"] is False

    def test_proxy_bound_passes_through(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = self._running_manager(cfg)
        mgr.assigned_ports["web"] = port
        with patch.object(
            mgr,
            "_proxy_listener_status",
            return_value={
                "bound": True,
                "port": TEST_PROXY_PORT,
                "reason": None,
                "routes": None,
            },
        ):
            data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["healthy"] is True
        assert task["last_health"]["method"] == "tcp"
        assert data["proxy"]["bound"] is True

    def test_no_proxy_check_when_no_host(self):
        cfg = _make_config(tasks={"web": {"command": "echo"}})
        mgr = _make_manager(cfg)
        with patch.object(mgr, "_is_pane_alive", return_value=True):
            data = mgr.list_tasks()
        # No host-routed task → no proxy block in output.
        assert "proxy" not in data

    def test_upstream_failure_takes_priority(self):
        # If upstream itself is down, the proxy override should NOT replace
        # the real upstream-failure reason.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()

        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = self._running_manager(cfg)
        mgr.assigned_ports["web"] = closed_port
        with patch.object(
            mgr,
            "_proxy_listener_status",
            return_value={
                "bound": False,
                "port": TEST_PROXY_PORT,
                "reason": "proxy down",
                "routes": None,
            },
        ):
            data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["healthy"] is False
        # Real failure preserved — method stays "tcp", not "proxy"
        assert task["last_health"]["method"] == "tcp"

    def test_route_missing_flips_healthy(self, http_server):
        # Proxy is bound and the daemon view is available, but THIS project's
        # host route isn't registered — the public URL would 502.
        port, _ = http_server
        cfg = _make_config(
            name="demo",
            tasks={"api": {"command": "echo", "host": "api"}},
        )
        mgr = self._running_manager(cfg)
        mgr.assigned_ports["api"] = port
        with patch.object(
            mgr,
            "_proxy_listener_status",
            return_value={
                "bound": True,
                "port": TEST_PROXY_PORT,
                "reason": None,
                "routes": {},
            },
        ):
            data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["healthy"] is False
        assert task["last_health"]["method"] == "proxy"
        assert "no route" in task["last_health"]["reason"]
        assert data["proxy"]["bound"] is True
        # The per-project routes map should NOT leak into status output.
        assert "routes" not in data["proxy"]

    def test_route_present_passes_through(self, http_server):
        port, _ = http_server
        cfg = _make_config(
            name="demo",
            tasks={"api": {"command": "echo", "host": "api"}},
        )
        mgr = self._running_manager(cfg)
        mgr.assigned_ports["api"] = port
        with patch.object(
            mgr,
            "_proxy_listener_status",
            return_value={
                "bound": True,
                "port": TEST_PROXY_PORT,
                "reason": None,
                "routes": {"api": port},
            },
        ):
            data = mgr.list_tasks()
        task = data["tasks"][0]
        assert task["healthy"] is True
        assert task["last_health"]["method"] == "tcp"


class TestProxyListenerStatusInternals:
    """_proxy_listener_status() prefers the daemon view, falls back to TCP."""

    def test_daemon_view_running_with_route(self):
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(
                mgr,
                "_query_daemon_proxy_routes",
                return_value={
                    "command": "proxy_routes",
                    "running": True,
                    "routes": {"demo": {"api": 5000}},
                },
            ),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = TEST_PROXY_PORT
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        assert info["bound"] is True
        assert info["routes"] == {"api": 5000}

    def test_daemon_view_proxy_not_running_priv_port(self):
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(
                mgr,
                "_query_daemon_proxy_routes",
                return_value={"command": "proxy_routes", "running": False, "routes": {}},
            ),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = 443
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        assert info["bound"] is False
        reason = info["reason"] or ""
        assert "isn't running" in reason
        # Privileged port → suggest sudo and surface the config option.
        assert "sudo taskmux daemon" in reason
        assert "proxy_https_port" in reason

    def test_daemon_view_proxy_not_running_unpriv_port(self):
        # When the configured port is non-privileged the message should NOT
        # tell the user to use sudo.
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(
                mgr,
                "_query_daemon_proxy_routes",
                return_value={"command": "proxy_routes", "running": False, "routes": {}},
            ),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = TEST_PROXY_PORT
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        reason = info["reason"] or ""
        assert "sudo" not in reason
        assert "privileged" not in reason
        assert "taskmux daemon" in reason

    def test_daemon_view_unknown_project_returns_empty_routes(self):
        # Daemon proxy is up but knows nothing about this project — surfaces
        # as bound=True with empty routes so list_tasks can flag the missing
        # route per task.
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(
                mgr,
                "_query_daemon_proxy_routes",
                return_value={
                    "command": "proxy_routes",
                    "running": True,
                    "routes": {"other": {"api": 1234}},
                },
            ),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = TEST_PROXY_PORT
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        assert info["bound"] is True
        assert info["routes"] == {}

    def test_daemon_unreachable_falls_back_to_tcp_success(self, http_server):
        port, _ = http_server
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(mgr, "_query_daemon_proxy_routes", return_value=None),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = port
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        assert info["bound"] is True
        assert info["routes"] is None  # unknown — daemon view wasn't available

    def test_wildcard_bind_probes_loopback(self, http_server):
        # http_server listens on 127.0.0.1. With proxy_bind=0.0.0.0 (LAN
        # exposure), we can't connect() to 0.0.0.0 — must probe loopback.
        port, _ = http_server
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(mgr, "_query_daemon_proxy_routes", return_value=None),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = port
            mock_cfg.return_value.proxy_bind = "0.0.0.0"
            info = mgr._proxy_listener_status()
        assert info["bound"] is True

    def test_daemon_unreachable_falls_back_to_tcp_failure(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()

        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with (
            patch.object(mgr, "_query_daemon_proxy_routes", return_value=None),
            patch("taskmux.global_config.loadGlobalConfig") as mock_cfg,
        ):
            mock_cfg.return_value.proxy_enabled = True
            mock_cfg.return_value.proxy_https_port = closed_port
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        assert info["bound"] is False
        assert "not bound" in (info["reason"] or "")

    def test_proxy_disabled(self):
        cfg = _make_config(name="demo", tasks={"api": {"command": "echo", "host": "api"}})
        mgr = _make_manager(cfg)
        with patch("taskmux.global_config.loadGlobalConfig") as mock_cfg:
            mock_cfg.return_value.proxy_enabled = False
            mock_cfg.return_value.proxy_https_port = TEST_PROXY_PORT
            mock_cfg.return_value.proxy_bind = "127.0.0.1"
            info = mgr._proxy_listener_status()
        assert info["bound"] is False
        assert "disabled" in (info["reason"] or "")


class TestIsHealthyBackcompat:
    """is_task_healthy still returns bool — existing callers unaffected."""

    def test_returns_bool(self, http_server):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        mgr = _make_manager(cfg)
        mgr.assigned_ports["web"] = port
        assert mgr.is_task_healthy("web") is True
