"""Tests for the unified daemon's WebSocket API and registry sync."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from pathlib import Path
from unittest.mock import patch

import pytest
import websockets

from taskmux import daemon as daemon_mod
from taskmux import paths as paths_mod
from taskmux import registry as reg


class FakeTmuxManager:
    """Minimal stand-in for TmuxManager — no real tmux contact."""

    def __init__(self, config, config_dir=None):  # noqa: ARG002
        self.config = config
        self.session = None

    def session_exists(self) -> bool:
        return False

    def auto_restart_tasks(self) -> None:
        pass

    def get_task_status(self, task_name: str) -> dict:
        return {"name": task_name, "running": False, "healthy": False}

    def restart_task(self, task_name: str) -> dict:
        return {"ok": True, "action": "restarted", "task": task_name}

    def kill_task(self, task_name: str) -> dict:
        return {"ok": True, "action": "killed", "task": task_name}

    def _get_session(self):
        raise RuntimeError("no real tmux in tests")


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Patch every taskmux path + TmuxManager for a hermetic daemon."""
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(paths_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(paths_mod, "CERTS_DIR", tmp_path / "certs")
    monkeypatch.setattr(paths_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_PID", tmp_path / "daemon.pid")
    monkeypatch.setattr(paths_mod, "GLOBAL_DAEMON_LOG", tmp_path / "daemon.log")
    monkeypatch.setattr(daemon_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setenv("TASKMUX_DISABLE_PROXY", "1")
    # Stub TmuxManager so we don't talk to real tmux
    from taskmux import cli as cli_mod

    monkeypatch.setattr(cli_mod, "TmuxManager", FakeTmuxManager)
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
    """End-to-end: spin daemon, register two projects, query list_projects."""
    port = _free_port()
    cfg_a = _seed_project(isolated, "alpha")
    cfg_b = _seed_project(isolated, "beta")
    reg.registerProject("alpha", cfg_a)
    reg.registerProject("beta", cfg_b)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        # Avoid signal handler reinstall messing with pytest
        with patch.object(daemon_mod.signal, "signal"):
            server_task = asyncio.create_task(daemon.start())
            try:
                await _wait_for_port(port)
                resp = await _ws_request(port, {"command": "list_projects"})
                sessions = sorted(p["session"] for p in resp["projects"])
                assert sessions == ["alpha", "beta"]
                assert all("config_path" in p for p in resp["projects"])

                # Session-scoped status
                resp = await _ws_request(
                    port, {"command": "status", "params": {"session": "alpha"}}
                )
                assert resp["session"] == "alpha"
                assert resp["data"]["session_name"] == "alpha"

                # Unknown session
                resp = await _ws_request(
                    port, {"command": "status", "params": {"session": "ghost"}}
                )
                assert resp.get("error") == "unknown_session"
            finally:
                daemon.stop()
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                    await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_daemon_picks_up_new_registry_entry(isolated):
    """Add a project after the daemon starts; daemon should auto-register it."""
    port = _free_port()
    cfg_a = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg_a)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        with patch.object(daemon_mod.signal, "signal"):
            server_task = asyncio.create_task(daemon.start())
            try:
                await _wait_for_port(port)
                # Register a second project after daemon is up
                cfg_b = _seed_project(isolated, "beta")
                reg.registerProject("beta", cfg_b)
                # Force sync directly (registry watcher would fire too, but be deterministic)
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
        with patch.object(daemon_mod.signal, "signal"):
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
    """Registry entry pointing to a non-existent toml is exposed as config_missing."""
    port = _free_port()
    cfg_real = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg_real)
    # Point a second registration at a path that doesn't exist on disk.
    bogus = isolated / "missing" / "taskmux.toml"
    # registerProject resolves the path but doesn't require existence.
    reg.registerProject("ghost", bogus)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        with patch.object(daemon_mod.signal, "signal"):
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


def test_resync_reconciles_routes_from_disk(isolated, tmp_path: Path):
    """resync re-reads state.json + tmux pane state and updates proxy routes."""

    class _RichFakeTmux:
        """Adds the assigned_ports + reload_state + window-list surface that
        real TmuxManager exposes — but no actual tmux contact."""

        def __init__(self, config, config_dir=None):  # noqa: ARG002
            self.config = config
            self.assigned_ports: dict[str, int] = {}
            self._windows: list[str] = []
            self._exists = False

        def session_exists(self) -> bool:
            return self._exists

        def list_windows(self) -> list[str]:
            return list(self._windows)

        def reload_state(self) -> None:
            from taskmux.paths import projectStatePath

            try:
                import json as _json

                data = _json.loads(projectStatePath(self.config.name).read_text())
                ports = data.get("assigned_ports", {})
                self.assigned_ports = {k: int(v) for k, v in ports.items()}
            except Exception:  # noqa: BLE001
                self.assigned_ports = {}

        def auto_restart_tasks(self) -> None:
            pass

        def get_task_status(self, task_name: str) -> dict:
            return {"name": task_name, "running": False, "healthy": False}

    from taskmux import cli as cli_mod

    cli_mod.TmuxManager = _RichFakeTmux  # type: ignore[assignment]

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
    # Pre-seed the project state file with an assigned port — simulates
    # a `taskmux start` in another process.
    state_dir = isolated / "projects" / "alpha"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text('{"assigned_ports": {"api": 12345}}')

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)

        class _FakeProxy:
            def __init__(self):
                self.routes: dict[tuple[str, str], int] = {}

            def set_route(self, project, host, p):
                self.routes[(project, host)] = p

            def drop_route(self, project, host):
                self.routes.pop((project, host), None)

        fake = _FakeProxy()
        daemon.proxy = fake  # type: ignore[assignment]

        with patch.object(daemon_mod.signal, "signal"):
            server_task = asyncio.create_task(daemon.start())
            try:
                await _wait_for_port(port)
                # Tell the daemon's TmuxManager the api window is up.
                tm = daemon.projects["alpha"].tmux
                tm._exists = True  # type: ignore[attr-defined]
                tm._windows = ["api"]  # type: ignore[attr-defined]

                resp = await _ws_request(
                    port, {"command": "resync", "params": {"session": "alpha"}}
                )
                assert resp["data"]["ok"] is True
                assert "api" in resp["data"]["added"]
                assert fake.routes[("alpha", "api")] == 12345

                # Now simulate the task being stopped.
                tm._windows = []  # type: ignore[attr-defined]
                resp = await _ws_request(
                    port, {"command": "resync", "params": {"session": "alpha"}}
                )
                assert "api" in resp["data"]["dropped"]
                assert ("alpha", "api") not in fake.routes
            finally:
                daemon.stop()
                server_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError, SystemExit):
                    await asyncio.wait_for(server_task, timeout=1.0)

    asyncio.run(run())


def test_deleting_config_marks_project_missing(isolated):
    """Deleting a registered project's taskmux.toml marks it config_missing."""
    port = _free_port()
    cfg = _seed_project(isolated, "alpha")
    reg.registerProject("alpha", cfg)

    async def run():
        daemon = daemon_mod.TaskmuxDaemon(api_port=port)
        with patch.object(daemon_mod.signal, "signal"):
            server_task = asyncio.create_task(daemon.start())
            try:
                await _wait_for_port(port)
                resp = await _ws_request(port, {"command": "list_projects"})
                assert resp["projects"][0]["state"] == "ok"

                # Delete the toml; daemon's per-project ConfigWatcher should react.
                cfg.unlink()
                # Drive the missing transition deterministically since watchdog
                # latency varies across platforms / FS implementations.
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
