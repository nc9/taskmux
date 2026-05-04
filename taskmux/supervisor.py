"""Process supervisor — daemon-owned, PTY-backed task runner.

Opens a PTY per task, spawns via asyncio.create_subprocess_exec with setsid
(own process group), drains the master fd into a timestamped log file with
rotation, and signal-escalates on stop.

Posix-only today (mac+linux). `make_supervisor()` raises on Windows; protocol
seam is in place so a `WindowsSupervisor` (ConPTY + Job Objects) drops in
without restructuring.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import platform
import pty
import re
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from .errors import ErrorCode, TaskmuxError
from .events import recordEvent
from .hooks import runHook
from .models import RestartPolicy, TaskConfig, TaskmuxConfig

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def _parseSize(size_str: str) -> int:
    upper = size_str.strip().upper()
    for suffix in sorted(_SIZE_UNITS, key=len, reverse=True):
        if upper.endswith(suffix):
            num = upper[: -len(suffix)].strip()
            return int(float(num) * _SIZE_UNITS[suffix])
    return int(upper)


def _parseSince(since_str: str) -> datetime:
    try:
        dt = datetime.fromisoformat(since_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass
    total_seconds = 0
    for match in re.finditer(r"(\d+)([dhms])", since_str.lower()):
        val, unit = int(match.group(1)), match.group(2)
        total_seconds += val * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    if total_seconds == 0:
        raise TaskmuxError(
            ErrorCode.INVALID_ARGUMENT,
            detail=f"Cannot parse --since value: {since_str!r}. Use e.g. '5m', '1h', '2d'.",
        )
    return datetime.now(UTC) - timedelta(seconds=total_seconds)


def _logPath(
    project: str,
    task_name: str,
    task_cfg: TaskConfig,
    worktree_id: str | None = None,
) -> Path:
    if task_cfg.log_file:
        return Path(task_cfg.log_file).expanduser()
    from .paths import taskLogPath

    return taskLogPath(project, task_name, worktree_id)


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    method: str
    reason: str | None
    at: float

    def to_dict(self) -> dict:
        return {"ok": self.ok, "method": self.method, "reason": self.reason, "at": self.at}


class RestartTracker:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, float]] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._manually_stopped: set[str] = set()
        self._last_health: dict[str, HealthResult] = {}

    def get(self, task_name: str) -> dict[str, float]:
        return self._data.get(task_name, {"count": 0, "last": 0.0})

    def record(self, task_name: str) -> None:
        info = self.get(task_name)
        self._data[task_name] = {"count": info["count"] + 1, "last": time.time()}

    def reset(self, task_name: str) -> None:
        self._data.pop(task_name, None)

    def record_health_failure(self, task_name: str) -> int:
        count = self._consecutive_failures.get(task_name, 0) + 1
        self._consecutive_failures[task_name] = count
        return count

    def reset_health_failures(self, task_name: str) -> None:
        self._consecutive_failures.pop(task_name, None)

    def mark_manually_stopped(self, task_name: str) -> None:
        self._manually_stopped.add(task_name)

    def clear_manually_stopped(self, task_name: str) -> None:
        self._manually_stopped.discard(task_name)

    def is_manually_stopped(self, task_name: str) -> bool:
        return task_name in self._manually_stopped

    def record_health_result(self, task_name: str, result: HealthResult) -> None:
        self._last_health[task_name] = result

    def last_health(self, task_name: str) -> HealthResult | None:
        return self._last_health.get(task_name)


def rotateLogs(log_path: Path, max_files: int) -> None:
    oldest = Path(f"{log_path}.{max_files}")
    if oldest.exists():
        oldest.unlink()
    for i in range(max_files - 1, 0, -1):
        src = Path(f"{log_path}.{i}")
        dst = Path(f"{log_path}.{i + 1}")
        if src.exists():
            src.rename(dst)
    if log_path.exists():
        log_path.rename(Path(f"{log_path}.1"))


def _make_log_annotator(
    public_url: str, port: int, throttle_s: float = 30.0
) -> Callable[[str], str | None]:
    """Return a per-line callback that emits a public-URL annotation.

    Triggers when a line contains `localhost:{port}` (word-bounded — port `123`
    won't match a `:1234` substring). Throttled so HMR / repeat prints don't
    spam: at most one annotation per `throttle_s` seconds.
    """
    pattern = re.compile(rf"\blocalhost:{port}\b")
    state = {"last": 0.0}
    annotation = f"[taskmux] ↳ public URL: {public_url}"

    def annotate(line: str) -> str | None:
        if not pattern.search(line):
            return None
        now = time.monotonic()
        if now - state["last"] < throttle_s:
            return None
        state["last"] = now
        return annotation

    return annotate


class LogWriter:
    """Append PTY-drained bytes to a log file as timestamped lines.

    Buffers partial lines across writes; rotates when file size hits max_bytes.

    Optional `banner` is emitted as a synthetic timestamped line at open time
    (use it to surface the task's public URL before any program output). Optional
    `annotator` is called with each real line; a non-None return is emitted as a
    follow-up timestamped line. Synthetic lines (banner + annotation output) are
    never themselves passed to the annotator.
    """

    def __init__(
        self,
        path: Path,
        max_bytes: int,
        max_files: int,
        banner: str | None = None,
        annotator: Callable[[str], str | None] | None = None,
    ):
        self.path = path
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._buf = bytearray()
        self._annotator = annotator
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a")  # noqa: SIM115
        if banner is not None:
            self._emit(banner)

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while True:
            try:
                idx = self._buf.index(b"\n")
            except ValueError:
                break
            line = bytes(self._buf[:idx]).decode("utf-8", errors="replace").rstrip("\r")
            del self._buf[: idx + 1]
            self._write_line(line)

    def _emit(self, line: str) -> None:
        now = datetime.now(UTC)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        record = f"{ts} {line}\n"
        # Pre-rotate when this record would cross max_bytes and the file is
        # non-empty: ensures synthetic lines emitted at open (banner) land in
        # the *active* log, not in `.log.1` if the existing log was near full.
        with contextlib.suppress(OSError):
            current = self._fh.tell()
            if current > 0 and current + len(record.encode("utf-8")) > self.max_bytes:
                self._fh.close()
                rotateLogs(self.path, self.max_files)
                self._fh = open(self.path, "a")  # noqa: SIM115
        self._fh.write(record)
        self._fh.flush()

    def _write_line(self, line: str) -> None:
        self._emit(line)
        if self._annotator is not None:
            extra = self._annotator(line)
            if extra is not None:
                self._emit(extra)

    def flush_buffer(self) -> None:
        if self._buf:
            line = bytes(self._buf).decode("utf-8", errors="replace").rstrip("\r")
            if line:
                self._write_line(line)
            self._buf.clear()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.flush_buffer()
        with contextlib.suppress(Exception):
            self._fh.close()


def readLogFile(log_path: Path, lines: int, grep: str | None, since: str | None) -> list[str]:
    try:
        all_lines = log_path.read_text().splitlines()
    except OSError:
        return []

    if since:
        since_dt = _parseSince(since)
        filtered = []
        for line in all_lines:
            if len(line) >= 23:
                try:
                    line_dt = datetime.fromisoformat(line[:23]).replace(tzinfo=UTC)
                    if line_dt < since_dt:
                        continue
                except ValueError:
                    pass
            filtered.append(line)
        all_lines = filtered

    if grep:
        all_lines = [ln for ln in all_lines if grep.lower() in ln.lower()]

    return all_lines[-lines:]


@dataclass
class TaskProcess:
    proc: asyncio.subprocess.Process
    master_fd: int
    pgid: int
    log_writer: LogWriter
    exit_task: asyncio.Task | None
    started_at: float
    exit_code: int | None = field(default=None)


def _public_internal_pair(
    host: str | None, internal_port: int | None, proxy_port: int
) -> tuple[int | None, int | None, str | None]:
    """Status-dict shape for (port, internal_port, internal_url).

    Host-bound tasks expose a public port (the proxy) plus a loopback listener;
    surfacing the listener under `internal_*` stops agents pairing the public
    `url` with the loopback port.
    """
    if host is None:
        return internal_port, None, None
    iurl = f"http://127.0.0.1:{internal_port}" if internal_port else None
    return proxy_port, internal_port, iurl


@runtime_checkable
class Supervisor(Protocol):
    config: TaskmuxConfig
    config_dir: Path | None
    project_id: str
    worktree_id: str | None
    assigned_ports: dict[str, int]
    restart_tracker: RestartTracker
    on_task_route_change: Callable[[str, str, str, int | None], None] | None

    async def start_task(self, task_name: str) -> dict: ...
    async def stop_task(self, task_name: str) -> dict: ...
    async def restart_task(self, task_name: str) -> dict: ...
    async def kill_task(self, task_name: str) -> dict: ...
    async def start_all(self) -> dict: ...
    async def stop_all(self, *, grace: float | None = None) -> dict: ...
    async def restart_all(self) -> dict: ...
    async def auto_restart_tasks(self) -> None: ...

    def session_exists(self) -> bool: ...
    def list_windows(self) -> list[str]: ...
    def list_tasks(self) -> dict: ...
    def inspect_task(self, task_name: str) -> dict: ...
    def get_task_status(self, task_name: str) -> dict: ...
    def check_health(self, task_name: str) -> HealthResult: ...
    def is_task_healthy(self, task_name: str) -> bool: ...
    def probe_upstream(self, task_name: str) -> HealthResult: ...
    def notify_upstream_dead(self, task_name: str) -> None: ...
    def getLogPath(self, task_name: str) -> Path | None: ...
    def reload_state(self) -> None: ...


class PosixSupervisor:
    """Asyncio-driven process supervisor (Darwin + Linux).

    Owns one PTY + one asyncio.subprocess.Process per task. Uses setsid so each
    task gets its own process group; killpg cleans up trees. Linux gets an
    optional PR_SET_PDEATHSIG=SIGTERM in the child preexec for cleaner orphan
    behavior than mac (where SIGKILL of the daemon orphans tasks).
    """

    def __init__(
        self,
        config: TaskmuxConfig,
        config_dir: Path | None = None,
        project_id: str | None = None,
        worktree_id: str | None = None,
    ):
        self.config = config
        self.config_dir = config_dir
        # `project_id` is the canonical session/registry/URL key. For primary
        # worktrees it's `config.name`; for linked worktrees it's
        # `config.name-{worktree_id}`. Defaulted to config.name for callers
        # that haven't been worktree-aware'd yet.
        self.project_id: str = project_id or config.name
        self.worktree_id: str | None = worktree_id
        self._tasks: dict[str, TaskProcess] = {}
        # Per-task lock — serializes start/stop/restart/kill for the same task
        # so two concurrent RPCs can't both pass the "not running" check and
        # spawn duplicate processes. See review R-002.
        self._task_locks: dict[str, asyncio.Lock] = {}
        self.task_health: dict = {}
        self.restart_tracker = RestartTracker()
        self.assigned_ports: dict[str, int] = self._load_state()
        self.on_task_route_change: Callable[[str, str, str, int | None], None] | None = None
        # Short-lived TCP-probe cache (per-task). Used by status display +
        # proxy hot path; the 1.5 s TTL keeps us from hammering localhost.
        self._upstream_cache: dict[str, HealthResult] = {}

    def _lock_for(self, task_name: str) -> asyncio.Lock:
        lock = self._task_locks.get(task_name)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_name] = lock
        return lock

    def _resolve_cwd(self, cwd: str | None) -> str | None:
        if not cwd:
            return cwd
        p = Path(cwd).expanduser()
        if p.is_absolute():
            return str(p)
        if self.config_dir is not None:
            return str((self.config_dir / p).resolve())
        return str(p)

    def _state_path(self) -> Path:
        from .paths import projectStatePath

        return projectStatePath(self.config.name, self.worktree_id)

    def _load_state(self) -> dict[str, int]:
        try:
            data = json.loads(self._state_path().read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        ports = data.get("assigned_ports", {})
        return {k: int(v) for k, v in ports.items() if isinstance(v, int | str)}

    def reload_state(self) -> None:
        self.assigned_ports = self._load_state()

    def _save_state(self) -> None:
        from .paths import ensureProjectDir

        ensureProjectDir(self.config.name, self.worktree_id)
        with contextlib.suppress(OSError):
            self._state_path().write_text(
                json.dumps({"assigned_ports": self.assigned_ports}, indent=2)
            )

    @staticmethod
    def _pick_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _ensure_port(self, task_name: str) -> int:
        existing = self.assigned_ports.get(task_name)
        if existing is not None:
            return existing
        port = self._pick_free_port()
        self.assigned_ports[task_name] = port
        self._save_state()
        return port

    def _emit_route(self, task_name: str, port: int | None) -> None:
        cb = self.on_task_route_change
        if cb is None:
            return
        cfg = self.config.tasks.get(task_name)
        if cfg is None or cfg.host is None:
            return
        with contextlib.suppress(Exception):
            cb(self.project_id, task_name, cfg.host, port)

    def _wrap_command(self, task_name: str, command: str) -> str:
        cfg = self.config.tasks.get(task_name)
        if cfg is None or cfg.host is None:
            return command
        port = self._ensure_port(task_name)
        return f"export PORT={port}; {command}"

    def _build_log_decor(
        self, task_name: str, task_cfg: TaskConfig
    ) -> tuple[str | None, Callable[[str], str | None] | None]:
        """Compose (banner, annotator) for a host-bound task's log file.

        Non-host-bound tasks get (None, None) — logs read identically to today.
        Wildcard hosts get the banner only (no fixed hostname to annotate).
        """
        if task_cfg.host is None:
            return None, None
        from .url import taskUrl

        port = self.assigned_ports.get(task_name)
        public_url = taskUrl(self.project_id, task_cfg.host)
        suffix = " (wildcard subdomain match)" if task_cfg.host == "*" else ""
        if port is None:
            banner = f"[taskmux] serving {public_url}{suffix}"
        else:
            banner = f"[taskmux] serving {public_url}{suffix} → http://localhost:{port}"
        if task_cfg.host == "*" or port is None:
            return banner, None
        return banner, _make_log_annotator(public_url, port)

    def _cleanup_port(self, port: int) -> None:
        try:
            result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
            for pid_str in result.stdout.strip().split("\n"):
                if pid_str.strip():
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.kill(int(pid_str.strip()), signal.SIGKILL)
        except OSError:
            pass

    def session_exists(self) -> bool:
        return bool(self._tasks)

    def list_windows(self) -> list[str]:
        return list(self._tasks.keys())

    def getLogPath(self, task_name: str) -> Path | None:
        if task_name not in self.config.tasks:
            return None
        path = _logPath(self.config.name, task_name, self.config.tasks[task_name], self.worktree_id)
        return path if path.exists() else None

    @staticmethod
    def _build_preexec() -> Callable[[], None] | None:
        if platform.system() != "Linux":
            return None
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1

            def preexec() -> None:
                libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)

            return preexec
        except OSError:
            return None

    async def _spawn(self, task_name: str) -> TaskProcess:
        task_cfg = self.config.tasks[task_name]
        cwd_abs = self._resolve_cwd(task_cfg.cwd)
        wrapped = self._wrap_command(task_name, task_cfg.command)

        master_fd, slave_fd = pty.openpty()
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                wrapped,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd_abs,
                start_new_session=True,
                preexec_fn=self._build_preexec(),
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid

        log_path = _logPath(self.config.name, task_name, task_cfg, self.worktree_id)
        banner, annotator = self._build_log_decor(task_name, task_cfg)
        log_writer = LogWriter(
            log_path,
            _parseSize(task_cfg.log_max_size),
            task_cfg.log_max_files,
            banner=banner,
            annotator=annotator,
        )

        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, lambda: self._on_pty_data(task_name, master_fd, log_writer))

        tp = TaskProcess(
            proc=proc,
            master_fd=master_fd,
            pgid=pgid,
            log_writer=log_writer,
            exit_task=None,
            started_at=time.time(),
        )
        tp.exit_task = asyncio.create_task(self._wait_for_exit(task_name, proc))
        self._tasks[task_name] = tp
        return tp

    def _on_pty_data(self, task_name: str, master_fd: int, log_writer: LogWriter) -> None:
        try:
            data = os.read(master_fd, 4096)
        except BlockingIOError:
            return
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                self._detach_reader(master_fd)
            return
        if not data:
            self._detach_reader(master_fd)
            return
        log_writer.write(data)

    def _detach_reader(self, master_fd: int) -> None:
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(master_fd)

    async def _wait_for_exit(self, task_name: str, proc: asyncio.subprocess.Process) -> None:
        exit_code = await proc.wait()
        self._on_task_exited(task_name, exit_code)

    def _on_task_exited(self, task_name: str, exit_code: int) -> None:
        tp = self._tasks.pop(task_name, None)
        if tp is None:
            return
        tp.exit_code = exit_code
        self._detach_reader(tp.master_fd)
        with contextlib.suppress(OSError):
            while True:
                data = os.read(tp.master_fd, 4096)
                if not data:
                    break
                tp.log_writer.write(data)
        with contextlib.suppress(OSError):
            os.close(tp.master_fd)
        tp.log_writer.close()
        cfg = self.config.tasks.get(task_name)
        if cfg is not None and cfg.host is not None:
            self._emit_route(task_name, None)
        recordEvent(
            "task_exited",
            session=self.config.name,
            task=task_name,
            exit_code=exit_code,
        )

    def _killpg(self, pgid: int, signum: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signum)

    async def _wait_proc_exit(self, proc: asyncio.subprocess.Process, timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _stop_one(self, task_name: str, grace: float) -> None:
        tp = self._tasks.get(task_name)
        if tp is None:
            return
        self._killpg(tp.pgid, signal.SIGINT)
        if not await self._wait_proc_exit(tp.proc, grace):
            self._killpg(tp.pgid, signal.SIGTERM)
            if not await self._wait_proc_exit(tp.proc, 3):
                self._killpg(tp.pgid, signal.SIGKILL)
                await self._wait_proc_exit(tp.proc, 1)
        if tp.exit_task is not None and not tp.exit_task.done():
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(tp.exit_task, timeout=2)

    def _err(self, code: ErrorCode, **kwargs: str | int) -> dict:
        err = TaskmuxError(code, **kwargs)
        return {"ok": False, "error_code": code.value, "error": err.message}

    def _toposort_tasks(self, task_names: list[str]) -> list[str]:
        in_degree: dict[str, int] = {n: 0 for n in task_names}
        dependents: dict[str, list[str]] = {n: [] for n in task_names}
        name_set = set(task_names)
        for name in task_names:
            for dep in self.config.tasks[name].depends_on:
                if dep in name_set:
                    in_degree[name] += 1
                    dependents[dep].append(name)

        queue: deque[str] = deque(n for n in task_names if in_degree[n] == 0)
        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for dep in dependents[node]:
                in_degree[dep] -= 1
                if in_degree[dep] == 0:
                    queue.append(dep)
        if len(result) != len(task_names):
            remaining = set(task_names) - set(result)
            raise TaskmuxError(ErrorCode.TASK_DEPENDENCY_CYCLE, dep=", ".join(sorted(remaining)))
        return result

    async def _wait_for_healthy(self, task_name: str, timeout: float) -> bool:
        task_cfg = self.config.tasks[task_name]
        interval = task_cfg.health_interval
        elapsed = 0.0
        while elapsed < timeout:
            if self.is_task_healthy(task_name):
                return True
            await asyncio.sleep(interval)
            elapsed += interval
        return self.is_task_healthy(task_name)

    async def start_task(self, task_name: str) -> dict:
        async with self._lock_for(task_name):
            return await self._start_task_locked(task_name)

    async def _start_task_locked(self, task_name: str) -> dict:
        self.restart_tracker.clear_manually_stopped(task_name)
        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)
        if task_name in self._tasks:
            return self._err(ErrorCode.TASK_ALREADY_RUNNING, task=task_name)

        task_cfg = self.config.tasks[task_name]

        prior_port = self.assigned_ports.get(task_name)
        if prior_port is not None:
            self._cleanup_port(prior_port)

        warnings: list[str] = []
        for dep in task_cfg.depends_on:
            if dep not in self._tasks:
                warnings.append(f"Dependency '{dep}' is not running")

        if not runHook(self.config.hooks.before_start, task_name):
            return self._err(ErrorCode.HOOK_FAILED, exit_code="n/a", command="global before_start")
        if not runHook(task_cfg.hooks.before_start, task_name):
            return self._err(
                ErrorCode.HOOK_FAILED, exit_code="n/a", command=f"{task_name} before_start"
            )

        await self._spawn(task_name)

        runHook(task_cfg.hooks.after_start, task_name)
        runHook(self.config.hooks.after_start, task_name)

        if task_cfg.host is not None:
            self._emit_route(task_name, self.assigned_ports.get(task_name))

        recordEvent("task_started", session=self.config.name, task=task_name)
        result: dict = {"ok": True, "task": task_name, "action": "started"}
        if warnings:
            result["warnings"] = warnings
        return result

    async def stop_task(self, task_name: str) -> dict:
        async with self._lock_for(task_name):
            return await self._stop_task_locked(task_name)

    async def _stop_task_locked(self, task_name: str) -> dict:
        self.restart_tracker.mark_manually_stopped(task_name)
        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)
        if task_name not in self._tasks:
            return self._err(ErrorCode.TASK_NOT_RUNNING, task=task_name)

        task_cfg = self.config.tasks[task_name]
        runHook(self.config.hooks.before_stop, task_name)
        runHook(task_cfg.hooks.before_stop, task_name)

        await self._stop_one(task_name, float(task_cfg.stop_grace_period))

        runHook(task_cfg.hooks.after_stop, task_name)
        runHook(self.config.hooks.after_stop, task_name)
        if task_cfg.host is not None:
            self._emit_route(task_name, None)
        recordEvent("task_stopped", session=self.config.name, task=task_name, reason="manual")
        return {"ok": True, "task": task_name, "action": "stopped"}

    async def restart_task(self, task_name: str) -> dict:
        async with self._lock_for(task_name):
            return await self._restart_task_locked(task_name)

    async def _restart_task_locked(self, task_name: str) -> dict:
        self.restart_tracker.clear_manually_stopped(task_name)
        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)

        task_cfg = self.config.tasks[task_name]
        if task_name in self._tasks:
            runHook(task_cfg.hooks.before_stop, task_name)
            await self._stop_one(task_name, float(task_cfg.stop_grace_period))
            runHook(task_cfg.hooks.after_stop, task_name)

        prior_port = self.assigned_ports.get(task_name)
        if prior_port is not None:
            self._cleanup_port(prior_port)

        runHook(task_cfg.hooks.before_start, task_name)
        await self._spawn(task_name)
        runHook(task_cfg.hooks.after_start, task_name)

        if task_cfg.host is not None:
            self._emit_route(task_name, self.assigned_ports.get(task_name))

        recordEvent("task_restarted", session=self.config.name, task=task_name)
        return {"ok": True, "task": task_name, "action": "restarted"}

    async def kill_task(self, task_name: str) -> dict:
        async with self._lock_for(task_name):
            return await self._kill_task_locked(task_name)

    async def _kill_task_locked(self, task_name: str) -> dict:
        self.restart_tracker.mark_manually_stopped(task_name)
        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)
        if task_name not in self._tasks:
            return self._err(ErrorCode.TASK_NOT_RUNNING, task=task_name)

        tp = self._tasks[task_name]
        self._killpg(tp.pgid, signal.SIGKILL)
        await self._wait_proc_exit(tp.proc, 1)
        if tp.exit_task is not None and not tp.exit_task.done():
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(tp.exit_task, timeout=2)

        cfg = self.config.tasks.get(task_name)
        if cfg is not None and cfg.host is not None:
            self._emit_route(task_name, None)
        recordEvent("task_killed", session=self.config.name, task=task_name)
        return {"ok": True, "task": task_name, "action": "killed"}

    async def start_all(self) -> dict:
        if self._tasks:
            return self._err(ErrorCode.SESSION_EXISTS, session=self.config.name)

        if not self.config.auto_start:
            return {
                "ok": True,
                "session": self.config.name,
                "action": "started",
                "tasks": [],
                "warnings": ["auto_start disabled, no tasks launched"],
            }

        auto_tasks = {name: cfg for name, cfg in self.config.tasks.items() if cfg.auto_start}
        if not auto_tasks:
            return self._err(
                ErrorCode.CONFIG_VALIDATION,
                detail="No auto-start tasks defined in config",
            )

        sorted_names = self._toposort_tasks(list(auto_tasks.keys()))

        if not runHook(self.config.hooks.before_start):
            return self._err(ErrorCode.HOOK_FAILED, exit_code="n/a", command="global before_start")

        started: list[str] = []
        warnings: list[str] = []
        for task_name in sorted_names:
            task_cfg = auto_tasks[task_name]

            skip = False
            for dep in task_cfg.depends_on:
                if dep in auto_tasks:
                    dep_cfg = auto_tasks[dep]
                    timeout = dep_cfg.health_retries * dep_cfg.health_interval
                    if not await self._wait_for_healthy(dep, timeout):
                        warnings.append(f"Dependency '{dep}' not healthy, skipping '{task_name}'")
                        skip = True
                        break
            if skip:
                continue

            # Per-task lock so a concurrent start_task RPC for the same name
            # can't race with us spawning here.
            async with self._lock_for(task_name):
                if task_name in self._tasks:
                    warnings.append(f"Task '{task_name}' already running, skipped")
                    continue

                runHook(task_cfg.hooks.before_start, task_name)

                prior_port = self.assigned_ports.get(task_name)
                if prior_port is not None:
                    self._cleanup_port(prior_port)

                await self._spawn(task_name)

                runHook(task_cfg.hooks.after_start, task_name)
                if task_cfg.host is not None:
                    self._emit_route(task_name, self.assigned_ports.get(task_name))
                started.append(task_name)

        runHook(self.config.hooks.after_start)
        recordEvent("session_started", session=self.config.name, tasks=started)

        result: dict = {
            "ok": True,
            "session": self.config.name,
            "action": "started",
            "tasks": started,
        }
        if warnings:
            result["warnings"] = warnings
        return result

    async def stop_all(self, *, grace: float | None = None) -> dict:
        for task_name in self.config.tasks:
            self.restart_tracker.mark_manually_stopped(task_name)

        if not self._tasks:
            return self._err(ErrorCode.SESSION_NOT_FOUND, session=self.config.name)

        runHook(self.config.hooks.before_stop)

        max_grace = (
            grace
            if grace is not None
            else max((cfg.stop_grace_period for cfg in self.config.tasks.values()), default=5)
        )

        for tp in list(self._tasks.values()):
            self._killpg(tp.pgid, signal.SIGINT)

        deadline = time.monotonic() + max_grace
        while time.monotonic() < deadline and self._tasks:
            await asyncio.sleep(0.1)

        for tp in list(self._tasks.values()):
            self._killpg(tp.pgid, signal.SIGTERM)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and self._tasks:
            await asyncio.sleep(0.1)

        for tp in list(self._tasks.values()):
            self._killpg(tp.pgid, signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and self._tasks:
            await asyncio.sleep(0.1)

        for task_name, task_cfg in self.config.tasks.items():
            runHook(task_cfg.hooks.after_stop, task_name)
            if task_cfg.host is not None:
                self._emit_route(task_name, None)

        runHook(self.config.hooks.after_stop)
        recordEvent("session_stopped", session=self.config.name)
        return {"ok": True, "session": self.config.name, "action": "stopped"}

    async def restart_all(self) -> dict:
        if self._tasks:
            await self.stop_all()
        for task_name in self.config.tasks:
            self.restart_tracker.clear_manually_stopped(task_name)
        result = await self.start_all()
        if result.get("ok"):
            result["action"] = "restarted"
        return result

    def get_task_status(self, task_name: str) -> dict:
        task_cfg = self.config.tasks.get(task_name)
        running = task_name in self._tasks
        status: dict[str, str | bool] = {
            "name": task_name,
            "running": running,
            "healthy": False,
            "state": "stopped",
            "command": task_cfg.command if task_cfg else "",
            "last_check": datetime.now().isoformat(),
        }
        if running:
            status["healthy"] = self.is_task_healthy(task_name)
            status["state"] = self._compute_state(task_name, task_cfg, bool(status["healthy"]))
        return status

    def _compute_state(self, task_name: str, task_cfg: TaskConfig | None, healthy: bool) -> str:
        """Map (running, healthy, host, boot window) → state label.

        - host-bound, port not answering, within boot_grace → "starting"
        - port answering (or no host)                       → "running"
        - host-bound, port not answering, past boot_grace   → "unhealthy"

        When the user configured an explicit `health_url` / `health_check`,
        that probe's verdict wins — `healthy=False` from a 500 response or a
        failing shell check must not be masked by an open TCP port.
        """
        if task_cfg is None or task_cfg.host is None:
            return "running" if healthy else "unhealthy"
        if task_cfg.health_url is not None or task_cfg.health_check is not None:
            return "running" if healthy else "unhealthy"
        probe = self.probe_upstream(task_name)
        if probe.ok:
            return "running"
        tp = self._tasks.get(task_name)
        if tp is not None and time.time() - tp.started_at < task_cfg.boot_grace:
            return "starting"
        return "unhealthy"

    def inspect_task(self, task_name: str) -> dict:
        from .global_config import loadGlobalConfig
        from .url import taskUrl

        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)

        task_cfg = self.config.tasks[task_name]
        url = taskUrl(self.project_id, task_cfg.host) if task_cfg.host is not None else None
        proxy_port = loadGlobalConfig().proxy_https_port
        port, internal_port, internal_url = _public_internal_pair(
            task_cfg.host, self.assigned_ports.get(task_name), proxy_port
        )
        info: dict = {
            "name": task_name,
            "command": task_cfg.command,
            "auto_start": task_cfg.auto_start,
            "restart_policy": str(task_cfg.restart_policy),
            "log_file": str(_logPath(self.config.name, task_name, task_cfg, self.worktree_id)),
            "cwd": task_cfg.cwd,
            "host": task_cfg.host,
            "url": url,
            "port": port,
            "internal_port": internal_port,
            "internal_url": internal_url,
            "tunnel": str(task_cfg.tunnel) if task_cfg.tunnel else None,
            "public_hostname": task_cfg.public_hostname,
            "public_url": (
                f"https://{task_cfg.public_hostname}/" if task_cfg.public_hostname else None
            ),
            "health_check": task_cfg.health_check,
            "depends_on": task_cfg.depends_on,
            "running": False,
            "healthy": False,
            "pid": None,
            "started_at": None,
        }
        tp = self._tasks.get(task_name)
        if tp is not None:
            info["running"] = True
            info["pid"] = tp.proc.pid
            info["started_at"] = tp.started_at
            result = self.check_health(task_name)
            info["healthy"] = result.ok
            info["last_health"] = result.to_dict()
        return info

    def list_tasks(self) -> dict:
        from .global_config import loadGlobalConfig
        from .url import taskUrl

        exists = self.session_exists()
        proxy_port = loadGlobalConfig().proxy_https_port
        tasks = []
        for task_name, task_cfg in self.config.tasks.items():
            status = self.get_task_status(task_name)
            last = self.restart_tracker.last_health(task_name)
            url = taskUrl(self.project_id, task_cfg.host) if task_cfg.host is not None else None
            public_url = (
                f"https://{task_cfg.public_hostname}/" if task_cfg.public_hostname else None
            )
            port, internal_port, internal_url = _public_internal_pair(
                task_cfg.host, self.assigned_ports.get(task_name), proxy_port
            )
            tasks.append(
                {
                    "name": task_name,
                    "running": status["running"],
                    "healthy": status["healthy"],
                    "state": status["state"],
                    "command": task_cfg.command,
                    "auto_start": task_cfg.auto_start,
                    "host": task_cfg.host,
                    "url": url,
                    "port": port,
                    "internal_port": internal_port,
                    "internal_url": internal_url,
                    "restart_policy": str(task_cfg.restart_policy),
                    "cwd": task_cfg.cwd,
                    "depends_on": task_cfg.depends_on,
                    "tunnel": str(task_cfg.tunnel) if task_cfg.tunnel else None,
                    "public_hostname": task_cfg.public_hostname,
                    "public_url": public_url,
                    "last_health": last.to_dict() if last else None,
                }
            )
        return {
            "session": self.config.name,
            "running": exists,
            "active_tasks": len(self._tasks),
            "tasks": tasks,
        }

    def _probe_http(
        self, url: str, timeout: float, expected_status: int, expected_body: str | None
    ) -> HealthResult:
        now = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                status = resp.status
                if status != expected_status:
                    return HealthResult(False, "http", f"status {status} != {expected_status}", now)
                if expected_body:
                    body = resp.read(64 * 1024).decode("utf-8", errors="replace")
                    if not re.search(expected_body, body):
                        return HealthResult(False, "http", "body mismatch", now)
                return HealthResult(True, "http", None, now)
        except urllib.error.HTTPError as e:
            return HealthResult(False, "http", f"status {e.code} != {expected_status}", now)
        except urllib.error.URLError as e:
            return HealthResult(False, "http", f"url error: {e.reason}", now)
        except TimeoutError:
            return HealthResult(False, "http", f"timeout after {timeout}s", now)
        except Exception as e:  # noqa: BLE001
            return HealthResult(False, "http", f"{type(e).__name__}: {e}", now)

    def _probe_tcp(self, port: int, timeout: float) -> HealthResult:
        now = time.time()
        try:
            with socket.create_connection(("localhost", port), timeout=timeout):
                return HealthResult(True, "tcp", None, now)
        except (TimeoutError, OSError) as e:
            return HealthResult(False, "tcp", f"connect refused: {e}", now)

    def probe_upstream(self, task_name: str) -> HealthResult:
        """Cached TCP probe of a host-bound task's assigned port.

        The proxy hot path and `taskmux status` both ask this question; the 1.5 s
        cache keeps us from opening a fresh socket per HTTP request while still
        catching a dead upstream within ~2 s. Returns a `no_host` result for
        tasks that don't have a host/port — caller distinguishes from a real
        TCP failure.
        """
        cfg = self.config.tasks.get(task_name)
        port = self.assigned_ports.get(task_name)
        now = time.time()
        if cfg is None or cfg.host is None or port is None:
            return HealthResult(False, "no_host", "no host/port for task", now)
        cached = self._upstream_cache.get(task_name)
        if cached is not None and now - cached.at < 1.5:
            return cached
        result = self._probe_tcp(port, 0.5)
        self._upstream_cache[task_name] = result
        return result

    def notify_upstream_dead(self, task_name: str) -> None:
        """Proxy → supervisor hook when forwarding hits ECONNREFUSED.

        Invalidates the cache so the next status check / health tick picks up
        the dead state immediately rather than waiting out the 1.5 s TTL.
        Restart decisions stay owned by `auto_restart_tasks` under its lock.
        """
        self._upstream_cache.pop(task_name, None)

    def _probe_shell(self, command: str, timeout: float) -> HealthResult:
        now = time.time()
        try:
            result = subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
            if result.returncode == 0:
                return HealthResult(True, "shell", None, now)
            return HealthResult(False, "shell", f"exit {result.returncode}", now)
        except subprocess.TimeoutExpired:
            return HealthResult(False, "shell", f"timeout after {timeout}s", now)
        except OSError as e:
            return HealthResult(False, "shell", f"OSError: {e}", now)

    def check_health(self, task_name: str) -> HealthResult:
        task_cfg = self.config.tasks.get(task_name)
        now = time.time()
        if not task_cfg:
            result = HealthResult(False, "none", "task not in config", now)
            self.restart_tracker.record_health_result(task_name, result)
            return result

        timeout = float(task_cfg.health_timeout)
        assigned_port = self.assigned_ports.get(task_name)
        if task_cfg.health_url:
            result = self._probe_http(
                task_cfg.health_url,
                timeout,
                task_cfg.health_expected_status,
                task_cfg.health_expected_body,
            )
        elif task_cfg.health_check:
            result = self._probe_shell(task_cfg.health_check, timeout)
        elif task_cfg.host is not None and assigned_port is not None:
            # Route the host-bound TCP path through probe_upstream so the proxy
            # hot path and `taskmux status` share a single 1.5 s cache window.
            result = self.probe_upstream(task_name)
        else:
            ok = task_name in self._tasks
            result = HealthResult(ok, "proc", None if ok else "process not running", now)

        self.restart_tracker.record_health_result(task_name, result)
        return result

    def is_task_healthy(self, task_name: str) -> bool:
        return self.check_health(task_name).ok

    def check_task_health(self, task_name: str) -> bool:
        is_healthy = self.is_task_healthy(task_name)
        self.task_health[task_name] = {
            "healthy": is_healthy,
            "last_check": datetime.now(),
            "status": self.get_task_status(task_name),
        }
        return is_healthy

    async def auto_restart_tasks(self) -> None:
        now = time.time()

        for task_name, task_cfg in self.config.tasks.items():
            if task_cfg.restart_policy == RestartPolicy.NO:
                continue
            if self.restart_tracker.is_manually_stopped(task_name):
                continue

            healthy = self.check_task_health(task_name)
            proc_alive = task_name in self._tasks

            if healthy:
                self.restart_tracker.reset_health_failures(task_name)
                info = self.restart_tracker.get(task_name)
                if info["count"] > 0 and now - info["last"] > 60:
                    self.restart_tracker.reset(task_name)
                continue

            should_restart = False

            if not proc_alive:
                should_restart = True
            elif task_cfg.restart_policy in (RestartPolicy.ON_FAILURE, RestartPolicy.ALWAYS):
                failures = self.restart_tracker.record_health_failure(task_name)
                recordEvent(
                    "health_check_failed",
                    session=self.config.name,
                    task=task_name,
                    attempt=failures,
                )
                # Host-bound TCP failures past the boot grace window are
                # unambiguous — one miss is enough. The general HTTP/shell path
                # keeps `health_retries` (default 3) for tolerance to flapping
                # apps, since those probes return false-negatives more often.
                tp = self._tasks.get(task_name)
                past_boot = tp is not None and now - tp.started_at >= task_cfg.boot_grace
                tcp_only = (
                    task_cfg.host is not None
                    and task_cfg.health_url is None
                    and task_cfg.health_check is None
                )
                threshold = (
                    task_cfg.health_retries_tcp
                    if tcp_only and past_boot
                    else task_cfg.health_retries
                )
                if failures >= threshold:
                    should_restart = True

            if not should_restart:
                continue

            info = self.restart_tracker.get(task_name)
            if task_cfg.max_restarts and info["count"] >= task_cfg.max_restarts:
                recordEvent(
                    "max_restarts_reached",
                    session=self.config.name,
                    task=task_name,
                    count=int(info["count"]),
                )
                continue

            delay = min(task_cfg.restart_backoff ** info["count"], 60)
            if info["last"] and now - info["last"] < delay:
                continue

            reason = "process_exited" if not proc_alive else "health_retries_exceeded"
            recordEvent("auto_restart", session=self.config.name, task=task_name, reason=reason)
            async with self._lock_for(task_name):
                await self._restart_task_locked(task_name)
            self.restart_tracker.record(task_name)
            self.restart_tracker.reset_health_failures(task_name)


def make_supervisor(
    config: TaskmuxConfig,
    config_dir: Path | None = None,
    project_id: str | None = None,
    worktree_id: str | None = None,
) -> Supervisor:
    sysname = platform.system()
    if sysname in ("Darwin", "Linux"):
        return PosixSupervisor(
            config,
            config_dir=config_dir,
            project_id=project_id,
            worktree_id=worktree_id,
        )
    raise NotImplementedError(
        f"No Supervisor implementation for platform {sysname!r}. "
        "Posix is supported today; a WindowsSupervisor (ConPTY + Job Objects) "
        "is the open seam."
    )
