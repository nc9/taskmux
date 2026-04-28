"""Thin RPC client for the taskmux daemon's WebSocket API.

Used by the CLI: each lifecycle command opens a short-lived WS connection,
sends one JSON request, and reads one JSON response. `ensure_daemon_running`
fires off a detached daemon if none is up and polls until it answers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import websockets

from .errors import ErrorCode, TaskmuxError
from .paths import ensureTaskmuxDir, globalDaemonLogPath


def _api_port() -> int:
    from .global_config import loadGlobalConfig

    return loadGlobalConfig().api_port


async def _send(port: int, payload: dict, timeout: float = 5.0) -> dict:
    async with websockets.connect(
        f"ws://localhost:{port}", open_timeout=timeout, close_timeout=timeout
    ) as ws:
        await ws.send(json.dumps(payload, default=str))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


def is_daemon_running() -> bool:
    from .daemon import get_daemon_pid

    return get_daemon_pid() is not None


def ensure_daemon_running(port: int | None = None, timeout: float = 8.0) -> int | None:
    """Ping; spawn detached if absent; poll until it answers `ping`."""
    from .daemon import get_daemon_pid

    p = port if port is not None else _api_port()
    pid = get_daemon_pid()
    if pid is not None:
        return pid

    ensureTaskmuxDir()
    log_fh = open(globalDaemonLogPath(), "ab")  # noqa: SIM115
    cmd = [sys.executable, "-m", "taskmux", "daemon"]
    if port is not None:
        cmd += ["--port", str(port)]
    subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pid = get_daemon_pid()
        if pid is not None:
            try:
                asyncio.run(_send(p, {"command": "ping"}, timeout=0.5))
                return pid
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.1)
    return None


def call(
    command: str,
    *,
    params: dict | None = None,
    port: int | None = None,
    timeout: float = 10.0,
    ensure: bool = True,
) -> dict:
    """One-shot WS request. Auto-spawns daemon when `ensure=True`."""
    p = port if port is not None else _api_port()
    if ensure and ensure_daemon_running(port=port) is None:
        raise TaskmuxError(
            ErrorCode.INTERNAL,
            detail="failed to start taskmux daemon — see ~/.taskmux/daemon.log",
        )
    payload: dict = {"command": command}
    if params:
        payload["params"] = params
    return asyncio.run(_send(p, payload, timeout=timeout))


def call_no_ensure(
    command: str,
    *,
    params: dict | None = None,
    port: int | None = None,
    timeout: float = 2.0,
) -> dict | None:
    """Best-effort RPC for read-only queries. Returns None if daemon absent."""
    if not is_daemon_running():
        return None
    try:
        return call(command, params=params, port=port, timeout=timeout, ensure=False)
    except Exception:  # noqa: BLE001
        return None


def follow_log_file(log_path: Path, grep: str | None = None) -> None:
    """Tail a log file (file is daemon-owned) — `tail -f` semantics, sync."""
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    try:
        with open(log_path) as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    line = line.rstrip("\n")
                    if grep and grep.lower() not in line.lower():
                        continue
                    console.print(escape(line))
                else:
                    time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped following logs[/dim]")


def follow_log_files(task_log_paths: list[tuple[str, Path, str]], grep: str | None = None) -> None:
    """Follow multiple log files concurrently; tag each line with task name + color."""
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    handles: list[tuple[str, object, str]] = []
    for task_name, log_path, color in task_log_paths:
        try:
            f = open(log_path)  # noqa: SIM115
            f.seek(0, 2)
            handles.append((task_name, f, color))
        except OSError:
            continue
    try:
        while True:
            any_output = False
            for task_name, f, color in handles:
                line = f.readline()  # type: ignore[union-attr]
                if line:
                    any_output = True
                    line = line.rstrip("\n")
                    if grep and grep.lower() not in line.lower():
                        continue
                    prefix = escape(f"[{task_name}]")
                    console.print(f"[{color}]{prefix}[/{color}] {escape(line)}")
            if not any_output:
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped following logs[/dim]")
    finally:
        for _, f, _ in handles:
            with __import__("contextlib").suppress(Exception):
                f.close()  # type: ignore[union-attr]
