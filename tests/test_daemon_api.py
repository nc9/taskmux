"""Tests for the unified daemon's WebSocket API and registry sync."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path

import pytest
import websockets

from taskmux import daemon as daemon_mod
from taskmux import paths as paths_mod
from taskmux import registry as reg
from taskmux.daemon import TaskmuxDaemon


class TestUpstreamDeadWakesHealthLoop:
    """ECONNREFUSED through the proxy must short-circuit the health-loop sleep
    so auto_restart_tasks runs within seconds rather than waiting out
    health_check_interval (default 30 s)."""

    def test_on_upstream_dead_sets_wakeup_event_and_notifies_supervisor(self):
        async def run():
            d = daemon_mod.TaskmuxDaemon(api_port=0)
            d._loop = asyncio.get_running_loop()
            d._health_wakeup = asyncio.Event()

            class _FakeSup:
                def __init__(self):
                    self.dead_calls: list[str] = []

                def notify_upstream_dead(self, task):
                    self.dead_calls.append(task)

            sup = _FakeSup()
            d.projects["alpha"] = sup  # type: ignore[assignment]

            d._on_upstream_dead("alpha", "api", "api-task")
            # call_soon_threadsafe schedules the .set(); yield once.
            await asyncio.sleep(0)

            assert d._health_wakeup.is_set()
            assert sup.dead_calls == ["api-task"]

        asyncio.run(run())

    def test_health_loop_wakes_early_on_signal(self):
        async def run():
            d = daemon_mod.TaskmuxDaemon(api_port=0)
            d._loop = asyncio.get_running_loop()
            d.health_check_interval = 30  # would normally block 30 s
            d.running = True
            sweeps = 0

            class _CountingSup:
                async def auto_restart_tasks(self):
                    nonlocal sweeps
                    sweeps += 1

            d.projects["alpha"] = _CountingSup()  # type: ignore[assignment]
            d.websocket_clients = set()  # skip broadcast path
            loop_task = asyncio.create_task(d._health_check_loop())
            try:
                # Let the first sweep run, then signal the wake-up.
                await asyncio.sleep(0.05)
                d._wake_health_loop()
                await asyncio.sleep(0.05)
            finally:
                d.running = False
                d._wake_health_loop()  # let the loop exit
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
            assert sweeps >= 2, (
                f"expected the wake-up to trigger a second sweep within 100 ms, got {sweeps}"
            )

        asyncio.run(run())


class TestNextSweepTimeout:
    """Periodic cadence drops to HOST_BOUND_TCP_INTERVAL when a host-bound
    TCP-only task is running, so detection doesn't wait 30s with no proxy
    traffic to trigger the wake-up."""

    @staticmethod
    def _daemon_with(cfg, running_tasks):
        d = daemon_mod.TaskmuxDaemon(api_port=0)
        d.health_check_interval = 30
        d.configs["alpha"] = cfg

        class _SupStub:
            def __init__(self, names):
                self._tasks = dict.fromkeys(names, object())

        d.projects["alpha"] = _SupStub(running_tasks)  # type: ignore[assignment]
        return d

    def test_cadence_drops_for_running_host_bound_tcp_task(self):
        from taskmux.models import TaskConfig, TaskmuxConfig

        cfg = TaskmuxConfig(
            name="alpha",
            tasks={"web": TaskConfig(command="echo", host="web")},
        )
        d = self._daemon_with(cfg, ["web"])
        snapshot = list(d.projects.items())
        assert d._next_sweep_timeout(snapshot) == float(
            daemon_mod.TaskmuxDaemon.HOST_BOUND_TCP_INTERVAL
        )

    def test_cadence_keeps_default_when_host_task_has_explicit_health(self):
        from taskmux.models import TaskConfig, TaskmuxConfig

        cfg = TaskmuxConfig(
            name="alpha",
            tasks={
                "web": TaskConfig(
                    command="echo",
                    host="web",
                    health_check="true",
                )
            },
        )
        d = self._daemon_with(cfg, ["web"])
        snapshot = list(d.projects.items())
        assert d._next_sweep_timeout(snapshot) == 30

    def test_cadence_keeps_default_when_host_task_not_running(self):
        from taskmux.models import TaskConfig, TaskmuxConfig

        cfg = TaskmuxConfig(
            name="alpha",
            tasks={"web": TaskConfig(command="echo", host="web")},
        )
        d = self._daemon_with(cfg, [])  # not running
        snapshot = list(d.projects.items())
        assert d._next_sweep_timeout(snapshot) == 30


class TestProxyBindTargets:
    """Plan dual-stack pairs so v6-preferring resolvers reach the proxy."""

    def test_v4_loopback_mirrors_v6(self):
        assert TaskmuxDaemon._proxy_bind_targets("127.0.0.1") == [
            (socket.AF_INET, "127.0.0.1"),
            (socket.AF_INET6, "::1"),
        ]

    def test_v6_loopback_mirrors_v4(self):
        assert TaskmuxDaemon._proxy_bind_targets("::1") == [
            (socket.AF_INET6, "::1"),
            (socket.AF_INET, "127.0.0.1"),
        ]

    def test_v4_wildcard_mirrors_v6_wildcard(self):
        assert TaskmuxDaemon._proxy_bind_targets("0.0.0.0") == [
            (socket.AF_INET, "0.0.0.0"),
            (socket.AF_INET6, "::"),
        ]

    def test_v6_wildcard_mirrors_v4_wildcard(self):
        assert TaskmuxDaemon._proxy_bind_targets("::") == [
            (socket.AF_INET6, "::"),
            (socket.AF_INET, "0.0.0.0"),
        ]

    def test_specific_v4_address_single_stack(self):
        assert TaskmuxDaemon._proxy_bind_targets("10.0.0.5") == [
            (socket.AF_INET, "10.0.0.5"),
        ]

    def test_specific_v6_address_single_stack(self):
        assert TaskmuxDaemon._proxy_bind_targets("fd00::1") == [
            (socket.AF_INET6, "fd00::1"),
        ]


class TestPreBindProxySockets:
    """Partial-fallback behavior when v6 bring-up fails."""

    def _ephemeral_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _make_daemon(self, port: int) -> TaskmuxDaemon:
        from taskmux.global_config import GlobalConfig

        d = TaskmuxDaemon.__new__(TaskmuxDaemon)
        d.global_config = GlobalConfig(proxy_bind="127.0.0.1", proxy_https_port=port)
        d.logger = daemon_mod.logging.getLogger("taskmux-daemon-test")
        return d

    def test_v6_socket_creation_failure_falls_back_to_v4(self, monkeypatch):
        """OSError from socket.socket(AF_INET6, ...) must NOT abort startup."""
        port = self._ephemeral_port()
        d = self._make_daemon(port)

        real_socket = socket.socket

        def fake_socket(family, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError("Address family not supported by protocol")
            return real_socket(family, *args, **kwargs)

        monkeypatch.setattr(daemon_mod.socket, "socket", fake_socket)

        try:
            socks = d._pre_bind_proxy_sockets()
            assert len(socks) == 1
            assert socks[0].family == socket.AF_INET
        finally:
            for s in socks:
                s.close()

    def test_v6_setsockopt_failure_falls_back_to_v4(self, monkeypatch):
        """OSError from IPV6_V6ONLY setsockopt must NOT abort startup."""
        port = self._ephemeral_port()
        d = self._make_daemon(port)

        real_socket = socket.socket

        class FakeV6Sock:
            def __init__(self, *a, **kw):
                self._real = real_socket(socket.AF_INET, socket.SOCK_STREAM)

            def setsockopt(self, level, optname, value):  # noqa: ARG002
                if level == socket.IPPROTO_IPV6:
                    raise OSError("Operation not supported")

            def close(self):
                self._real.close()

        def fake_socket(family, *args, **kwargs):
            if family == socket.AF_INET6:
                return FakeV6Sock(family, *args, **kwargs)
            return real_socket(family, *args, **kwargs)

        monkeypatch.setattr(daemon_mod.socket, "socket", fake_socket)

        try:
            socks = d._pre_bind_proxy_sockets()
            assert len(socks) == 1
            assert socks[0].family == socket.AF_INET
        finally:
            for s in socks:
                s.close()

    def test_primary_bind_failure_aborts(self, monkeypatch):
        """When v4 (primary) fails, return [] — daemon can't serve any traffic."""
        port = self._ephemeral_port()
        d = self._make_daemon(port)

        real_socket = socket.socket

        def fake_socket(family, *args, **kwargs):
            if family == socket.AF_INET:
                raise OSError("EACCES")
            return real_socket(family, *args, **kwargs)

        monkeypatch.setattr(daemon_mod.socket, "socket", fake_socket)
        assert d._pre_bind_proxy_sockets() == []


class FakeSupervisor:
    """Minimal stand-in for PosixSupervisor — no real processes."""

    def __init__(
        self,
        config,
        config_dir=None,  # noqa: ARG002
        project_id: str | None = None,
        worktree_id: str | None = None,
    ):
        self.config = config
        self.project_id = project_id or config.name
        self.worktree_id = worktree_id
        self.assigned_ports: dict[str, int] = {}
        self.on_task_route_change = None
        self._exists = False
        self._windows: list[str] = []

    def session_exists(self) -> bool:
        return self._exists

    def list_windows(self) -> list[str]:
        return list(self._windows)

    def reload_state(self) -> None:
        from taskmux.paths import projectStatePath

        try:
            data = json.loads(projectStatePath(self.config.name, self.worktree_id).read_text())
            ports = data.get("assigned_ports", {})
            self.assigned_ports = {k: int(v) for k, v in ports.items()}
        except Exception:  # noqa: BLE001
            self.assigned_ports = {}

    async def auto_restart_tasks(self) -> None:
        return None

    def get_task_status(self, task_name: str) -> dict:
        return {"name": task_name, "running": False, "healthy": False}

    def list_tasks(self) -> dict:
        return {
            "session": self.config.name,
            "running": self._exists,
            "active_tasks": 0,
            "tasks": [],
        }

    def inspect_task(self, task_name: str) -> dict:
        return {"name": task_name, "running": False}

    def check_health(self, task_name: str):  # noqa: ANN201
        from taskmux.supervisor import HealthResult

        return HealthResult(False, "proc", "fake", 0.0)

    async def restart_task(self, task_name: str) -> dict:
        return {"ok": True, "action": "restarted", "task": task_name}

    async def kill_task(self, task_name: str) -> dict:
        return {"ok": True, "action": "killed", "task": task_name}

    async def start_task(self, task_name: str) -> dict:
        return {"ok": True, "action": "started", "task": task_name}

    async def stop_task(self, task_name: str) -> dict:
        return {"ok": True, "action": "stopped", "task": task_name}

    async def start_all(self) -> dict:
        return {"ok": True, "session": self.config.name, "action": "started", "tasks": []}

    async def stop_all(self, *, grace=None) -> dict:  # noqa: ARG002
        return {"ok": True, "session": self.config.name, "action": "stopped"}

    async def restart_all(self) -> dict:
        return {"ok": True, "session": self.config.name, "action": "restarted"}

    def getLogPath(self, task_name: str):  # noqa: ANN201
        return None


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Patch every taskmux path + supervisor factory for a hermetic daemon."""
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(paths_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(paths_mod, "CERTS_DIR", tmp_path / "certs")
    monkeypatch.setattr(paths_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_PID", tmp_path / "daemon.pid")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_LOG", tmp_path / "daemon.log")
    monkeypatch.setattr(daemon_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setenv("TASKMUX_DISABLE_PROXY", "1")
    monkeypatch.setattr(daemon_mod, "make_supervisor", FakeSupervisor)
    (tmp_path / "projects").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seed_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / "src" / name
    proj.mkdir(parents=True, exist_ok=True)
    cfg = proj / "taskmux.toml"
    cfg.write_text(
        f"""name = "{name}"
[tasks.web]
command = "echo hi"
"""
    )
    return cfg


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _ws_request(port: int, payload: dict, *, timeout: float = 2.0) -> dict:
    async with websockets.connect(
        f"ws://localhost:{port}", open_timeout=timeout, close_timeout=timeout
    ) as ws:
        await ws.send(json.dumps(payload))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _wait_for_port(port: int, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            await w.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"port {port} never came up")


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.05):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def test_daemon_serves_list_projects(isolated):
    port = _free_port()
    cfg_a = _seed_project(isolated, "alpha")
    cfg_b = _seed_project(isolated, "beta")
    reg.registerProject("alpha", cfg_a)
    reg.registerProject("beta", cfg_b)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            resp = await _ws_request(port, {"command": "list_projects"})
            sessions = sorted(p["session"] for p in resp["projects"])
            assert sessions == ["alpha", "beta"]
            assert all("config_path" in p for p in resp["projects"])

            resp = await _ws_request(port, {"command": "status", "params": {"session": "alpha"}})
            assert resp["session"] == "alpha"
            assert resp["data"]["session_name"] == "alpha"

            resp = await _ws_request(port, {"command": "status", "params": {"session": "ghost"}})
            assert resp.get("error") == "unknown_session"
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_daemon_picks_up_new_registry_entry(isolated):
    port = _free_port()
    cfg_a = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg_a)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            cfg_b = _seed_project(isolated, "beta")
            reg.registerProject("beta", cfg_b)
            await daemon._sync_with_registry()
            ok = await _wait_until(lambda: "beta" in daemon.projects, timeout=2.0)
            assert ok
            resp = await _ws_request(port, {"command": "list_projects"})
            sessions = sorted(p["session"] for p in resp["projects"])
            assert sessions == ["alpha", "beta"]
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_daemon_handles_unknown_command(isolated):
    port = _free_port()

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            resp = await _ws_request(port, {"command": "not_a_thing"})
            assert "unknown_command" in resp.get("error", "")
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_list_projects_includes_config_missing_entry(isolated):
    port = _free_port()
    cfg_real = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg_real)
    bogus = isolated / "missing" / "taskmux.toml"
    reg.registerProject("ghost", bogus)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            resp = await _ws_request(port, {"command": "list_projects"})
            rows = {p["session"]: p for p in resp["projects"]}
            assert rows["alpha"]["state"] == "ok"
            assert rows["ghost"]["state"] == "config_missing"
            assert rows["ghost"]["session_exists"] is False
            assert rows["ghost"]["task_count"] == 0
            assert str(bogus.resolve()) in rows["ghost"]["config_path"]
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_resync_reconciles_routes_from_disk(isolated):
    port = _free_port()
    proj = isolated / "src" / "alpha"
    proj.mkdir(parents=True, exist_ok=True)
    cfg = proj / "taskmux.toml"
    cfg.write_text(
        """name = "alpha"
[tasks.api]
command = "echo hi"
host = "api"
"""
    )
    reg.registerProject("alpha", cfg)
    state_dir = isolated / "projects" / "alpha"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text('{"assigned_ports": {"api": 12345}}')

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)

        class _FakeProxy:
            def __init__(self):
                self.routes: dict[tuple[str, str], int] = {}

            def set_route(self, project, host, p, task=None):
                del task  # fake doesn't track reverse-lookup
                self.routes[(project, host)] = p

            def drop_route(self, project, host):
                self.routes.pop((project, host), None)

            def routes_snapshot(self) -> dict[str, dict[str, int]]:
                out: dict[str, dict[str, int]] = {}
                for (project, host), port in self.routes.items():
                    out.setdefault(project, {})[host] = port
                return out

        fake = _FakeProxy()
        daemon.proxy = fake  # type: ignore[assignment]

        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            sup = daemon.projects["alpha"]
            sup._exists = True  # type: ignore[attr-defined]
            sup._windows = ["api"]  # type: ignore[attr-defined]

            resp = await _ws_request(port, {"command": "resync", "params": {"session": "alpha"}})
            assert resp["data"]["ok"] is True
            assert "api" in resp["data"]["added"]
            assert fake.routes[("alpha", "api")] == 12345

            sup._windows = []  # type: ignore[attr-defined]
            resp = await _ws_request(port, {"command": "resync", "params": {"session": "alpha"}})
            assert "api" in resp["data"]["dropped"]
            assert ("alpha", "api") not in fake.routes
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_deleting_config_marks_project_missing(isolated):
    port = _free_port()
    cfg = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            resp = await _ws_request(port, {"command": "list_projects"})
            assert resp["projects"][0]["state"] == "ok"

            cfg.unlink()
            await daemon._mark_missing("alpha")

            resp = await _ws_request(port, {"command": "list_projects"})
            row = resp["projects"][0]
            assert row["state"] == "config_missing"
            assert row["session_exists"] is False
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_lifecycle_commands_route_through_supervisor(isolated):
    """start/stop/restart/kill/inspect/health are async-routed via supervisor."""
    port = _free_port()
    cfg = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            for cmd in ("start", "stop", "restart", "kill"):
                resp = await _ws_request(
                    port, {"command": cmd, "params": {"session": "alpha", "task": "web"}}
                )
                assert resp["session"] == "alpha"
                assert resp["result"]["ok"] is True

            resp = await _ws_request(
                port, {"command": "inspect", "params": {"session": "alpha", "task": "web"}}
            )
            assert resp["result"]["name"] == "web"

            resp = await _ws_request(
                port, {"command": "health", "params": {"session": "alpha", "task": "web"}}
            )
            assert resp["result"]["method"] in ("proc", "tcp", "http", "shell")

            for cmd in ("start_all", "stop_all", "restart_all"):
                resp = await _ws_request(port, {"command": cmd, "params": {"session": "alpha"}})
                assert resp["result"]["ok"] is True
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_ping(isolated):
    port = _free_port()

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            resp = await _ws_request(port, {"command": "ping"})
            assert resp["ok"] is True
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_config_missing_then_recreate_recovers(isolated):
    """R-001 — a config that disappears and reappears must re-register."""
    port = _free_port()
    bogus = isolated / "missing" / "taskmux.toml"
    reg.registerProject("ghost", bogus)  # path doesn't exist yet

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        server_task = asyncio.create_task(daemon.start())
        try:
            await _wait_for_port(port)
            # Initially config_missing.
            resp = await _ws_request(port, {"command": "list_projects"})
            row = next(p for p in resp["projects"] if p["session"] == "ghost")
            assert row["state"] == "config_missing"

            # Create the config; sync_registry must re-register it as ok.
            bogus.parent.mkdir(parents=True, exist_ok=True)
            bogus.write_text('name = "ghost"\n[tasks.web]\ncommand = "echo hi"\n')
            await daemon._sync_with_registry()

            resp = await _ws_request(port, {"command": "list_projects"})
            row = next(p for p in resp["projects"] if p["session"] == "ghost")
            assert row["state"] == "ok", f"expected ok after recreate, got {row}"
        finally:
            daemon.stop()
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())
