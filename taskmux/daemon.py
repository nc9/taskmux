"""Unified multi-project daemon for Taskmux.

A single daemon process per host manages all registered projects:
  - Each project's `taskmux.toml` is loaded into its own TaskmuxCLI/TmuxManager.
  - The registry at ~/.taskmux/registry.json is watched for add/remove events.
  - Each project's config file is watched independently.
  - Health-check loop iterates all projects, applies per-task restart policy.
  - WebSocket API serves session-scoped requests + cross-project queries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import websockets
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .events import recordEvent
from .paths import (
    REGISTRY_PATH,
    ensureTaskmuxDir,
    globalDaemonLogPath,
    globalDaemonPidPath,
)
from .registry import readRegistry

if TYPE_CHECKING:
    from .cli import TaskmuxCLI


# ---------------------------------------------------------------------------
# PID-file helpers (global daemon)
# ---------------------------------------------------------------------------


def get_daemon_pid() -> int | None:
    """Return live global daemon PID, else None. Cleans stale pid file."""
    pid_path = globalDaemonPidPath()
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        with contextlib.suppress(OSError):
            pid_path.unlink()
        return None
    except OSError:
        return pid


def _write_daemon_pid() -> None:
    ensureTaskmuxDir()
    globalDaemonPidPath().write_text(str(os.getpid()))


def _clear_daemon_pid() -> None:
    pid_path = globalDaemonPidPath()
    with contextlib.suppress(OSError):
        if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()


# ---------------------------------------------------------------------------
# Per-project config watcher
# ---------------------------------------------------------------------------


class ConfigWatcher(FileSystemEventHandler):
    """Watches a single project's taskmux.toml and reloads its CLI on change."""

    def __init__(
        self,
        cli: TaskmuxCLI,
        loop: asyncio.AbstractEventLoop,
        on_reload: callable | None = None,  # type: ignore[type-arg]
        on_missing: callable | None = None,  # type: ignore[type-arg]
    ):
        self.cli = cli
        self.target_path = str(cli.config_path)
        self.loop = loop
        self.on_reload = on_reload
        self.on_missing = on_missing
        self.logger = logging.getLogger("taskmux-daemon")

    def _matches(self, event: FileSystemEvent) -> bool:
        if str(event.src_path) == self.target_path:
            return True
        # os.replace / atomic rename can fire moved events with dest=target.
        dest = getattr(event, "dest_path", None)
        return dest is not None and str(dest) == self.target_path

    def on_modified(self, event: FileSystemEvent) -> None:
        if not self._matches(event):
            return
        self.loop.call_soon_threadsafe(self._reload_safe)

    def on_created(self, event: FileSystemEvent) -> None:
        if not self._matches(event):
            return
        self.loop.call_soon_threadsafe(self._reload_safe)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Renamed onto the target → reload; renamed away → mark missing.
        dest = getattr(event, "dest_path", None)
        if dest is not None and str(dest) == self.target_path:
            self.loop.call_soon_threadsafe(self._reload_safe)
        elif str(event.src_path) == self.target_path:
            self.loop.call_soon_threadsafe(self._missing_safe)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if str(event.src_path) != self.target_path:
            return
        self.loop.call_soon_threadsafe(self._missing_safe)

    def _reload_safe(self) -> None:
        try:
            self.cli.reload_config()
            recordEvent("config_reloaded", session=self.cli.config.name)
            self.logger.info(f"Reloaded config for '{self.cli.config.name}'")
            if self.on_reload:
                self.on_reload(self.cli)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Failed to reload config at {self.target_path}: {e}")

    def _missing_safe(self) -> None:
        self.logger.warning(
            f"Config for '{self.cli.config.name}' disappeared at {self.target_path}"
        )
        if self.on_missing:
            self.on_missing(self.cli)


# ---------------------------------------------------------------------------
# Registry watcher
# ---------------------------------------------------------------------------


class RegistryWatcher(FileSystemEventHandler):
    """Watches ~/.taskmux/registry.json and notifies the daemon to re-sync."""

    def __init__(self, daemon: TaskmuxDaemon, loop: asyncio.AbstractEventLoop):
        self.daemon = daemon
        self.loop = loop
        self.target_path = str(REGISTRY_PATH)

    def _matches(self, event: FileSystemEvent) -> bool:
        # os.replace fires a moved event with dest_path = target.
        if str(event.src_path) == self.target_path:
            return True
        dest = getattr(event, "dest_path", None)
        return dest is not None and str(dest) == self.target_path

    def _schedule_sync(self) -> None:
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.daemon._sync_with_registry())
        )

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()

    def on_created(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()

    def on_moved(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class TaskmuxDaemon:
    """Unified multi-project daemon."""

    def __init__(self, api_port: int | None = None):
        from .global_config import loadGlobalConfig

        self.global_config = loadGlobalConfig()
        # Explicit api_port arg wins over global config so --port still works.
        self.api_port = api_port if api_port is not None else self.global_config.api_port
        self.running = False
        self.health_check_interval = self.global_config.health_check_interval
        self.health_check_task: asyncio.Task | None = None
        self.websocket_clients: set = set()
        self.projects: dict[str, TaskmuxCLI] = {}
        self.observers: dict[str, Observer] = {}  # type: ignore[reportInvalidTypeForm]
        self.registry_observer: Observer | None = None  # type: ignore[reportInvalidTypeForm]
        self.project_states: dict[str, str] = {}  # session -> "ok" | "config_missing" | "error"
        self.project_paths: dict[str, str] = {}   # session -> abs config_path
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.logger = self._setup_logging()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    # ---- logging ----

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("taskmux-daemon")
        logger.setLevel(logging.INFO)
        # Don't propagate to root — root may have its own handlers (basicConfig
        # set by deps), which would duplicate every record into our file/console.
        logger.propagate = False
        # Idempotent: drop any prior handlers before re-attaching.
        for h in list(logger.handlers):
            logger.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()

        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # When detached, the spawn redirects stderr → daemon.log already, so a
        # console (stderr) handler would double every line in the file. Only
        # attach the console handler when running in a real terminal.
        if sys.stderr.isatty():
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console.setFormatter(formatter)
            logger.addHandler(console)

        ensureTaskmuxDir()
        file_h = logging.FileHandler(globalDaemonLogPath())
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(formatter)
        logger.addHandler(file_h)
        return logger

    def _signal_handler(self, signum, frame) -> None:  # noqa: ARG002
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)

    # ---- lifecycle ----

    async def start(self) -> None:
        existing = get_daemon_pid()
        if existing is not None and existing != os.getpid():
            self.logger.error(f"Global daemon already running (pid {existing})")
            sys.exit(1)

        _write_daemon_pid()
        self.running = True
        self._loop = asyncio.get_running_loop()

        self.logger.info(f"Starting unified taskmux daemon (pid {os.getpid()})")

        await self._sync_with_registry()
        self._start_registry_watcher()

        self.health_check_task = asyncio.create_task(self._health_check_loop())
        api_task = asyncio.create_task(self._start_api_server())

        self.logger.info(
            f"Daemon ready on port {self.api_port} ({len(self.projects)} project(s))"
        )

        try:
            await asyncio.gather(self.health_check_task, api_task)
        except asyncio.CancelledError:
            self.logger.info("Daemon tasks cancelled")

    def stop(self) -> None:
        self.running = False

        if self.registry_observer is not None:
            with contextlib.suppress(Exception):
                self.registry_observer.stop()
                self.registry_observer.join(timeout=2)

        for session, observer in list(self.observers.items()):
            with contextlib.suppress(Exception):
                observer.stop()
                observer.join(timeout=2)
            self.observers.pop(session, None)

        if self.health_check_task and not self.health_check_task.done():
            self.health_check_task.cancel()

        _clear_daemon_pid()
        self.logger.info("Taskmux daemon stopped")

    # ---- registry sync ----

    def _start_registry_watcher(self) -> None:
        if self._loop is None:
            return
        ensureTaskmuxDir()
        observer = Observer()
        observer.schedule(
            RegistryWatcher(self, self._loop),
            str(REGISTRY_PATH.parent),
            recursive=False,
        )
        observer.start()
        self.registry_observer = observer
        self.logger.info(f"Watching registry at {REGISTRY_PATH}")

    async def _sync_with_registry(self) -> None:
        """Diff in-memory projects against registry on disk; add/remove as needed."""
        async with self._lock:
            on_disk = readRegistry()
            current = set(self.projects.keys())
            wanted = set(on_disk.keys())

            for session in wanted - current:
                entry = on_disk[session]
                self._register_locked(session, Path(entry["config_path"]))

            for session in current - wanted:
                self._unregister_locked(session)

    def _register_locked(self, session: str, config_path: Path) -> None:
        """Register a project. Caller must hold self._lock."""
        from .cli import TaskmuxCLI

        # Always remember the path so config_missing entries can surface it.
        self.project_paths[session] = str(config_path)

        if session in self.projects:
            return
        if not config_path.exists():
            self.logger.warning(
                f"Registry entry for '{session}' points to missing {config_path}"
            )
            self.project_states[session] = "config_missing"
            return
        try:
            cli = TaskmuxCLI(config_path=config_path)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Failed to load '{session}' from {config_path}: {e}")
            self.project_states[session] = "error"
            return

        if cli.config.name != session:
            self.logger.warning(
                f"Registry session '{session}' != config name '{cli.config.name}' "
                f"for {config_path}; using registry key"
            )

        self.projects[session] = cli
        self.project_states[session] = "ok"

        if self._loop is not None:
            observer = Observer()
            handler = ConfigWatcher(
                cli,
                self._loop,
                on_missing=lambda c, s=session: self._loop.call_soon_threadsafe(  # type: ignore[union-attr]
                    lambda: asyncio.create_task(self._mark_missing(s))
                ),
            )
            observer.schedule(handler, str(config_path.parent), recursive=False)
            observer.start()
            self.observers[session] = observer

        self.logger.info(f"Registered project '{session}' from {config_path}")

    def _unregister_locked(self, session: str) -> None:
        """Unregister a project. Caller must hold self._lock."""
        observer = self.observers.pop(session, None)
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.stop()
                observer.join(timeout=2)
        self.projects.pop(session, None)
        self.project_states.pop(session, None)
        self.project_paths.pop(session, None)
        self.logger.info(f"Unregistered project '{session}'")

    async def _mark_missing(self, session: str) -> None:
        """Mark a project as config_missing — drop the live CLI but keep the entry."""
        async with self._lock:
            observer = self.observers.pop(session, None)
            if observer is not None:
                with contextlib.suppress(Exception):
                    observer.stop()
                    observer.join(timeout=2)
            self.projects.pop(session, None)
            self.project_states[session] = "config_missing"
            self.logger.info(
                f"Project '{session}' marked config_missing — health checks paused"
            )

    # ---- health loop ----

    async def _health_check_loop(self) -> None:
        while self.running:
            try:
                async with self._lock:
                    snapshot = list(self.projects.items())
                for session, cli in snapshot:
                    try:
                        if cli.tmux.session_exists():
                            cli.tmux.auto_restart_tasks()
                    except Exception as e:  # noqa: BLE001
                        self.logger.error(f"Health check error for '{session}': {e}")

                if self.websocket_clients:
                    payload = await self._aggregate_status()
                    await self._broadcast_to_clients({"type": "health_check", "data": payload})

                await asyncio.sleep(self.health_check_interval)
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"Health check loop error: {e}")
                await asyncio.sleep(5)

    # ---- WebSocket API ----

    async def _start_api_server(self) -> None:
        async def handle_client(websocket) -> None:  # type: ignore[type-arg]
            self.websocket_clients.add(websocket)
            self.logger.info(f"WebSocket client connected: {websocket.remote_address}")
            try:
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        response = await self._handle_api_request(data)
                        await websocket.send(json.dumps(response))
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({"error": "Invalid JSON"}))
                    except Exception as e:  # noqa: BLE001
                        await websocket.send(json.dumps({"error": str(e)}))
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self.websocket_clients.discard(websocket)
                self.logger.info(f"WebSocket client disconnected: {websocket.remote_address}")

        async with websockets.serve(handle_client, "localhost", self.api_port):  # type: ignore[arg-type]
            await asyncio.Future()

    KNOWN_COMMANDS = frozenset(
        {"list_projects", "status_all", "status", "restart", "kill", "logs"}
    )

    async def _handle_api_request(self, data: dict) -> dict:
        command = data.get("command")
        params = data.get("params", {}) or {}

        if command not in self.KNOWN_COMMANDS:
            return {"error": "unknown_command", "command": command}

        if command == "list_projects":
            return {"command": command, "projects": await self._list_projects()}

        if command == "status_all":
            return {"command": command, "data": await self._aggregate_status()}

        # Session-scoped commands
        session = params.get("session")
        if not session:
            return {"error": "missing_session", "command": command}
        cli = self.projects.get(session)
        if cli is None:
            return {"error": "unknown_session", "session": session, "command": command}

        if command == "status":
            return {"command": command, "session": session, "data": self._project_status(cli)}

        if command == "restart":
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            result = cli.tmux.restart_task(task_name)
            return {"command": command, "session": session, "result": result}

        if command == "kill":
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            result = cli.tmux.kill_task(task_name)
            return {"command": command, "session": session, "result": result}

        if command == "logs":
            task_name = params.get("task")
            lines = params.get("lines", 100)
            if not task_name:
                return {"error": "missing_task", "session": session}
            try:
                if not cli.tmux.session_exists():
                    return {"error": "session_not_running", "session": session}
                sess = cli.tmux._get_session()
                window = sess.windows.get(window_name=task_name, default=None)
                if window and window.active_pane:
                    output = window.active_pane.cmd(
                        "capture-pane", "-p", "-S", f"-{lines}"
                    ).stdout
                    return {"command": command, "session": session, "logs": output}
            except Exception as e:  # noqa: BLE001
                return {"error": str(e), "session": session}
            return {"error": "could_not_retrieve_logs", "session": session}

        return {"error": "unknown_command", "command": command}

    # ---- status helpers ----

    def _project_status(self, cli: TaskmuxCLI) -> dict:
        session_exists = cli.tmux.session_exists()
        tasks: dict[str, dict] = {}
        for task_name in cli.config.tasks:
            tasks[task_name] = cli.tmux.get_task_status(task_name)
        return {
            "session_name": cli.config.name,
            "session_exists": session_exists,
            "tasks": tasks,
            "config_path": str(cli.config_path),
            "timestamp": datetime.now().isoformat(),
        }

    async def _aggregate_status(self) -> dict:
        async with self._lock:
            sessions = self._all_known_sessions_locked()
            loaded = dict(self.projects)
            states = dict(self.project_states)
            paths = dict(self.project_paths)
        out_projects = []
        for session in sessions:
            cli = loaded.get(session)
            state = states.get(session, "ok" if cli else "config_missing")
            if cli is None:
                out_projects.append(
                    {
                        "session": session,
                        "state": state,
                        "config_path": paths.get(session, ""),
                    }
                )
                continue
            try:
                out_projects.append(
                    {"session": session, "state": state} | self._project_status(cli)
                )
            except Exception as e:  # noqa: BLE001
                out_projects.append({"session": session, "state": "error", "error": str(e)})
        return {
            "projects": out_projects,
            "count": len(out_projects),
            "timestamp": datetime.now().isoformat(),
        }

    async def _list_projects(self) -> list[dict]:
        async with self._lock:
            sessions = self._all_known_sessions_locked()
            loaded = dict(self.projects)
            states = dict(self.project_states)
            paths = dict(self.project_paths)
        out: list[dict] = []
        for session in sessions:
            cli = loaded.get(session)
            state = states.get(session, "ok" if cli else "config_missing")
            row: dict = {
                "session": session,
                "config_path": str(cli.config_path) if cli else paths.get(session, ""),
                "state": state,
            }
            if cli is not None:
                row["session_exists"] = cli.tmux.session_exists()
                row["task_count"] = len(cli.config.tasks)
            else:
                row["session_exists"] = False
                row["task_count"] = 0
            out.append(row)
        return out

    def _all_known_sessions_locked(self) -> list[str]:
        """Union of registry entries + currently loaded projects, sorted."""
        on_disk = readRegistry()
        return sorted(set(on_disk.keys()) | set(self.projects.keys()))

    async def _broadcast_to_clients(self, message: dict) -> None:
        if not self.websocket_clients:
            return
        payload = json.dumps(message)
        disconnected = set()
        for client in self.websocket_clients:
            try:
                await client.send(payload)
            except Exception:  # noqa: BLE001
                disconnected.add(client)
        self.websocket_clients -= disconnected


# ---------------------------------------------------------------------------
# Non-daemon helpers
# ---------------------------------------------------------------------------


class SimpleConfigWatcher:
    """Simple config file watcher for `taskmux watch` (non-daemon mode)."""

    def __init__(self, taskmux_cli: TaskmuxCLI):
        self.taskmux_cli = taskmux_cli

    def watch_config(self) -> None:
        print("Watching taskmux.toml for changes...")
        print("Press Ctrl+C to stop")

        loop = asyncio.new_event_loop()
        observer = Observer()
        observer.schedule(
            ConfigWatcher(self.taskmux_cli, loop),
            str(self.taskmux_cli.config_path.parent),
            recursive=False,
        )
        observer.start()

        try:
            while True:
                # Drain any pending callbacks scheduled by watcher events.
                loop.call_soon(loop.stop)
                loop.run_forever()
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            print("\nStopped watching")

        observer.join()
        loop.close()


def list_running_projects() -> list[dict]:
    """Return the registry contents annotated with daemon status.

    Used by `taskmux daemon list` when querying without a live daemon.
    """
    from .registry import listRegistered

    out: list[dict] = []
    for entry in listRegistered():
        out.append({
            "session": entry["session"],
            "config_path": entry["config_path"],
            "registered_at": entry["registered_at"],
        })
    return out


# Backwards-compat shim — referenced by older code paths during the transition.
def list_running_daemons() -> list[dict]:
    """Deprecated: returns the registered project list (single global daemon now)."""
    return list_running_projects()
