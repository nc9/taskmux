"""Tmux session and task management."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import signal as sig
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import libtmux
from rich.console import Console
from rich.markup import escape

from .errors import ErrorCode, TaskmuxError
from .events import recordEvent
from .hooks import runHook
from .models import RestartPolicy, TaskConfig, TaskmuxConfig

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}


def _parseSize(size_str: str) -> int:
    """Parse human-readable size string to bytes. E.g. '10MB' -> 10485760."""
    upper = size_str.strip().upper()
    for suffix in sorted(_SIZE_UNITS, key=len, reverse=True):
        if upper.endswith(suffix):
            num = upper[: -len(suffix)].strip()
            return int(float(num) * _SIZE_UNITS[suffix])
    return int(upper)


def _logPath(session_name: str, task_name: str, task_cfg: TaskConfig) -> Path:
    """Resolve log file path for a task."""
    if task_cfg.log_file:
        return Path(task_cfg.log_file).expanduser()
    return Path.home() / ".taskmux" / "logs" / session_name / f"{task_name}.log"


def _parseSince(since_str: str) -> datetime:
    """Parse --since value: ISO timestamp or duration like '5m', '1h', '2d'."""
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


SHELL_NAMES = frozenset(("bash", "zsh", "sh", "fish"))

TASK_COLORS = ["cyan", "green", "yellow", "magenta", "blue", "red"]


@dataclass(frozen=True)
class HealthResult:
    """Outcome of a single health probe."""

    ok: bool
    method: str  # "http", "shell", "tcp", "pane", "none"
    reason: str | None
    at: float

    def to_dict(self) -> dict:
        return {"ok": self.ok, "method": self.method, "reason": self.reason, "at": self.at}


class RestartTracker:
    """Tracks per-task restart counts, health failures, and manual-stop state."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, float]] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._manually_stopped: set[str] = set()
        self._last_health: dict[str, HealthResult] = {}

    def get(self, task_name: str) -> dict[str, float]:
        return self._data.get(task_name, {"count": 0, "last": 0.0})

    def record(self, task_name: str) -> None:
        info = self.get(task_name)
        self._data[task_name] = {
            "count": info["count"] + 1,
            "last": time.time(),
        }

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


def _find_new_lines(current: list[str], prev_tail: list[str]) -> list[str]:
    """Return lines in current that are new since prev_tail."""
    if not prev_tail:
        return current
    target = prev_tail[-1]
    for i in range(len(current) - 1, -1, -1):
        if current[i] == target:
            ctx = min(len(prev_tail), i + 1)
            if current[i - ctx + 1 : i + 1] == prev_tail[-ctx:]:
                return current[i + 1 :]
    return current  # no match, prev scrolled away — return all


class TmuxManager:
    """Manages tmux sessions and tasks using libtmux API."""

    def __init__(self, config: TaskmuxConfig):
        self.config = config
        self.server = libtmux.Server()
        self.session: libtmux.Session | None = None
        self.task_health: dict = {}
        self.restart_tracker = RestartTracker()
        self._refresh_session()

    def _refresh_session(self) -> None:
        """Refresh session object from server"""
        try:
            self.session = self.server.sessions.get(session_name=self.config.name)
        except Exception:
            self.session = None

    def session_exists(self) -> bool:
        """Check if tmux session exists"""
        self._refresh_session()
        return self.session is not None

    def _get_session(self) -> libtmux.Session:
        """Get session, raising if it doesn't exist."""
        assert self.session is not None
        return self.session

    def _attach_log_pipe(self, pane: libtmux.Pane, task_name: str) -> None:
        """Attach pipe-pane to mirror pane output to a timestamped log file."""
        task_cfg = self.config.tasks[task_name]
        log_path = _logPath(self.config.name, task_name, task_cfg)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = _parseSize(task_cfg.log_max_size)
        max_files = task_cfg.log_max_files
        cmd_str = (
            f"exec python3 -u -m taskmux._log_pipe "
            f"{shlex.quote(str(log_path))} {max_bytes} {max_files}"
        )
        pane.cmd("pipe-pane", cmd_str)

    def getLogPath(self, task_name: str) -> Path | None:
        """Return log file path if it exists on disk."""
        if task_name not in self.config.tasks:
            return None
        path = _logPath(self.config.name, task_name, self.config.tasks[task_name])
        return path if path.exists() else None

    def list_windows(self) -> list[str]:
        """List all windows in the session"""
        if not self.session_exists():
            return []
        try:
            return [w.window_name for w in self._get_session().windows if w.window_name]
        except Exception:
            return []

    def _is_pane_alive(self, task_name: str) -> bool:
        """Check if task's pane has a running process (not just a shell)."""
        if not self.session_exists():
            return False
        try:
            window = self._get_session().windows.get(window_name=task_name, default=None)
            if window and window.active_pane:
                cmd = getattr(window.active_pane, "pane_current_command", "")
                return cmd != "" and cmd not in SHELL_NAMES
        except Exception:
            pass
        return False

    def _wait_for_exit(self, pane: libtmux.Pane, timeout: float) -> bool:
        """Poll pane_current_command until it returns to a shell or timeout."""
        elapsed = 0.0
        while elapsed < timeout:
            time.sleep(0.5)
            elapsed += 0.5
            cmd = getattr(pane, "pane_current_command", "")
            if cmd == "" or cmd in SHELL_NAMES:
                return True
        return False

    def _get_pane_child_pid(self, pane: libtmux.Pane) -> int | None:
        """Get the child process PID running inside the pane's shell."""
        shell_pid = getattr(pane, "pane_pid", None)
        if not shell_pid:
            return None
        try:
            result = subprocess.run(
                ["pgrep", "-P", str(shell_pid)],
                capture_output=True,
                text=True,
            )
            pids = result.stdout.strip().split("\n")
            return int(pids[0]) if pids and pids[0] else None
        except (ValueError, OSError):
            return None

    def _kill_process_tree(self, pid: int, signal_num: int = sig.SIGKILL) -> None:
        """Kill process and all children via process group."""
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal_num)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _cleanup_port(self, port: int) -> None:
        """Kill any process listening on port."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
            )
            for pid_str in result.stdout.strip().split("\n"):
                if pid_str.strip():
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.kill(int(pid_str.strip()), sig.SIGKILL)
        except OSError:
            pass

    def _probe_http(
        self,
        url: str,
        timeout: float,
        expected_status: int,
        expected_body: str | None,
    ) -> HealthResult:
        """HTTP GET; pass if status matches and (if set) body contains expected_body."""
        now = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                status = resp.status
                if status != expected_status:
                    return HealthResult(
                        False, "http", f"status {status} != {expected_status}", now
                    )
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
        except Exception as e:
            return HealthResult(False, "http", f"{type(e).__name__}: {e}", now)

    def _probe_tcp(self, port: int, timeout: float) -> HealthResult:
        """Open a TCP connection to localhost:port; pass if accepted within timeout."""
        now = time.time()
        try:
            with socket.create_connection(("localhost", port), timeout=timeout):
                return HealthResult(True, "tcp", None, now)
        except (TimeoutError, OSError) as e:
            return HealthResult(False, "tcp", f"connect refused: {e}", now)

    def _probe_shell(self, command: str, timeout: float) -> HealthResult:
        """Run shell command; exit 0 = healthy."""
        now = time.time()
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return HealthResult(True, "shell", None, now)
            return HealthResult(False, "shell", f"exit {result.returncode}", now)
        except subprocess.TimeoutExpired:
            return HealthResult(False, "shell", f"timeout after {timeout}s", now)
        except OSError as e:
            return HealthResult(False, "shell", f"OSError: {e}", now)

    def check_health(self, task_name: str) -> HealthResult:
        """Probe task health with precedence: health_url → health_check → tcp(port) → pane-alive."""
        task_cfg = self.config.tasks.get(task_name)
        now = time.time()
        if not task_cfg:
            result = HealthResult(False, "none", "task not in config", now)
            self.restart_tracker.record_health_result(task_name, result)
            return result

        timeout = float(task_cfg.health_timeout)
        if task_cfg.health_url:
            result = self._probe_http(
                task_cfg.health_url,
                timeout,
                task_cfg.health_expected_status,
                task_cfg.health_expected_body,
            )
        elif task_cfg.health_check:
            result = self._probe_shell(task_cfg.health_check, timeout)
        elif task_cfg.port:
            result = self._probe_tcp(task_cfg.port, timeout)
        else:
            ok = self._is_pane_alive(task_name)
            result = HealthResult(
                ok, "pane", None if ok else "pane shell-only or missing", now
            )

        self.restart_tracker.record_health_result(task_name, result)
        return result

    def is_task_healthy(self, task_name: str) -> bool:
        """Check task health. Returns bool; see check_health() for full result."""
        return self.check_health(task_name).ok

    def get_task_status(self, task_name: str) -> dict[str, str | bool]:
        """Get detailed status for a task"""
        task_cfg = self.config.tasks.get(task_name)
        status: dict[str, str | bool] = {
            "name": task_name,
            "running": False,
            "healthy": False,
            "command": task_cfg.command if task_cfg else "",
            "last_check": datetime.now().isoformat(),
        }

        if not self.session_exists():
            return status

        windows = self.list_windows()
        status["running"] = task_name in windows

        if status["running"]:
            status["healthy"] = self.is_task_healthy(task_name)

        return status

    def _send_command_to_window(
        self, sess: libtmux.Session, task_name: str, command: str, cwd: str | None = None
    ) -> libtmux.Window:
        """Create a new window and send a command to it."""
        kwargs: dict = {"attach": False, "window_name": task_name}
        if cwd:
            kwargs["start_directory"] = cwd
        window = sess.new_window(**kwargs)
        pane = window.active_pane
        if pane:
            pane.send_keys(command, enter=True)
            self._attach_log_pipe(pane, task_name)
        return window

    def _toposort_tasks(self, task_names: list[str]) -> list[str]:
        """Topological sort tasks by depends_on (Kahn's algorithm). Raises on cycles."""
        # Build adjacency + in-degree for the subset
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

    def _wait_for_healthy(self, task_name: str, timeout: float) -> bool:
        """Poll is_task_healthy until True or timeout."""
        task_cfg = self.config.tasks[task_name]
        interval = task_cfg.health_interval
        elapsed = 0.0
        while elapsed < timeout:
            if self.is_task_healthy(task_name):
                return True
            time.sleep(interval)
            elapsed += interval
        return self.is_task_healthy(task_name)

    def _err(self, code: ErrorCode, **kwargs: str | int) -> dict:
        """Build an error result dict with code and formatted message."""
        err = TaskmuxError(code, **kwargs)
        return {"ok": False, "error_code": code.value, "error": err.message}

    def start_task(self, task_name: str) -> dict:
        """Start a single task (create window + send command)."""
        self.restart_tracker.clear_manually_stopped(task_name)
        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)

        if not self.session_exists():
            # Create empty session first
            self.session = self.server.new_session(session_name=self.config.name, attach=False)

        sess = self._get_session()
        task_cfg = self.config.tasks[task_name]

        # Kill anything occupying the port before starting
        if task_cfg.port:
            self._cleanup_port(task_cfg.port)

        # Check if already running
        existing = sess.windows.get(window_name=task_name, default=None)
        if existing:
            return self._err(ErrorCode.TASK_ALREADY_RUNNING, task=task_name)

        # Warn if deps aren't running
        warnings: list[str] = []
        for dep in task_cfg.depends_on:
            if dep not in self.list_windows():
                warnings.append(f"Dependency '{dep}' is not running")

        # Hooks: global before_start, then task before_start
        if not runHook(self.config.hooks.before_start, task_name):
            return self._err(ErrorCode.HOOK_FAILED, exit_code="n/a", command="global before_start")
        if not runHook(task_cfg.hooks.before_start, task_name):
            return self._err(
                ErrorCode.HOOK_FAILED, exit_code="n/a", command=f"{task_name} before_start"
            )

        # If session was just created, rename default window instead of creating new
        if len(sess.windows) == 1 and sess.windows[0].window_name != task_name:
            default = sess.windows[0]
            # Only reuse if it's the placeholder default window
            if default.window_name in ("bash", "zsh", "sh", "fish"):
                default.rename_window(task_name)
                pane = default.active_pane
                if pane:
                    if task_cfg.cwd:
                        pane.send_keys(f"cd {task_cfg.cwd}", enter=True)
                    pane.send_keys(task_cfg.command, enter=True)
                    self._attach_log_pipe(pane, task_name)
            else:
                self._send_command_to_window(sess, task_name, task_cfg.command, task_cfg.cwd)
        else:
            self._send_command_to_window(sess, task_name, task_cfg.command, task_cfg.cwd)

        runHook(task_cfg.hooks.after_start, task_name)
        runHook(self.config.hooks.after_start, task_name)

        recordEvent("task_started", session=self.config.name, task=task_name)
        result: dict = {"ok": True, "task": task_name, "action": "started"}
        if warnings:
            result["warnings"] = warnings
        return result

    def stop_task(self, task_name: str) -> dict:
        """Graceful stop with signal escalation: C-c → SIGTERM → SIGKILL."""
        self.restart_tracker.mark_manually_stopped(task_name)
        if not self.session_exists():
            return self._err(ErrorCode.SESSION_NOT_FOUND, session=self.config.name)

        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)

        sess = self._get_session()
        window = sess.windows.get(window_name=task_name, default=None)
        if not window:
            return self._err(ErrorCode.TASK_NOT_RUNNING, task=task_name)

        task_cfg = self.config.tasks[task_name]

        # Hooks: global before_stop, then task before_stop
        runHook(self.config.hooks.before_stop, task_name)
        runHook(task_cfg.hooks.before_stop, task_name)

        pane = window.active_pane
        if pane:
            # Phase 1: SIGINT (Ctrl+C)
            pane.send_keys("C-c")

            if not self._wait_for_exit(pane, timeout=task_cfg.stop_grace_period):
                # Phase 2: SIGTERM via process group
                pid = self._get_pane_child_pid(pane)
                if pid:
                    self._kill_process_tree(pid, sig.SIGTERM)

                if not self._wait_for_exit(pane, timeout=3):
                    # Phase 3: SIGKILL entire process group
                    if pid:
                        self._kill_process_tree(pid, sig.SIGKILL)
                    # Final wait for cleanup
                    self._wait_for_exit(pane, timeout=1)

        # Hooks: task after_stop, then global after_stop
        runHook(task_cfg.hooks.after_stop, task_name)
        runHook(self.config.hooks.after_stop, task_name)
        recordEvent("task_stopped", session=self.config.name, task=task_name, reason="manual")
        return {"ok": True, "task": task_name, "action": "stopped"}

    def start_all(self) -> dict:
        """Start all auto_start tasks in dependency order."""
        if self.session_exists():
            return self._err(ErrorCode.SESSION_EXISTS, session=self.config.name)

        if not self.config.auto_start:
            # Create empty session, no tasks
            self.session = self.server.new_session(session_name=self.config.name, attach=False)
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
                ErrorCode.CONFIG_VALIDATION, detail="No auto-start tasks defined in config"
            )

        # Topological sort for dependency ordering
        sorted_names = self._toposort_tasks(list(auto_tasks.keys()))

        # Global before_start
        if not runHook(self.config.hooks.before_start):
            return self._err(ErrorCode.HOOK_FAILED, exit_code="n/a", command="global before_start")

        self.session = self.server.new_session(session_name=self.config.name, attach=False)
        sess = self._get_session()

        started: list[str] = []
        warnings: list[str] = []
        first = True
        for task_name in sorted_names:
            task_cfg = auto_tasks[task_name]

            # Wait for dependencies to become healthy before starting
            skip = False
            for dep in task_cfg.depends_on:
                if dep in auto_tasks:
                    dep_cfg = auto_tasks[dep]
                    timeout = dep_cfg.health_retries * dep_cfg.health_interval
                    if not self._wait_for_healthy(dep, timeout):
                        warnings.append(f"Dependency '{dep}' not healthy, skipping '{task_name}'")
                        skip = True
                        break
            if skip:
                continue

            runHook(task_cfg.hooks.before_start, task_name)

            if first and sess.windows:
                # First task reuses default window
                default_window = sess.windows[0]
                default_window.rename_window(task_name)
                pane = default_window.active_pane
                if pane:
                    if task_cfg.cwd:
                        pane.send_keys(f"cd {task_cfg.cwd}", enter=True)
                    pane.send_keys(task_cfg.command, enter=True)
                    self._attach_log_pipe(pane, task_name)
                first = False
            else:
                self._send_command_to_window(sess, task_name, task_cfg.command, task_cfg.cwd)

            runHook(task_cfg.hooks.after_start, task_name)
            started.append(task_name)

        # Global after_start
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

    def stop_all(self) -> dict:
        """Stop all tasks with signal escalation then kill session."""
        for task_name in self.config.tasks:
            self.restart_tracker.mark_manually_stopped(task_name)

        if not self.session_exists():
            return self._err(ErrorCode.SESSION_NOT_FOUND, session=self.config.name)

        # Global before_stop
        runHook(self.config.hooks.before_stop)

        # Phase 1: send C-c to all tasks
        sess = self._get_session()
        pane_map: dict[str, tuple[libtmux.Pane, int | None]] = {}
        for task_name, task_cfg in self.config.tasks.items():
            window = sess.windows.get(window_name=task_name, default=None)
            if window:
                runHook(task_cfg.hooks.before_stop, task_name)
                pane = window.active_pane
                if pane:
                    pane.send_keys("C-c")
                    pid = self._get_pane_child_pid(pane)
                    pane_map[task_name] = (pane, pid)

        # Wait for graceful exit (use max grace period across tasks)
        max_grace = max((cfg.stop_grace_period for cfg in self.config.tasks.values()), default=5)
        time.sleep(max_grace)

        # Phase 2: SIGTERM then SIGKILL any survivors
        for _name, (pane, pid) in pane_map.items():
            cmd = getattr(pane, "pane_current_command", "")
            if cmd and cmd not in SHELL_NAMES and pid:
                self._kill_process_tree(pid, sig.SIGTERM)

        time.sleep(1)

        for _name, (pane, pid) in pane_map.items():
            cmd = getattr(pane, "pane_current_command", "")
            if cmd and cmd not in SHELL_NAMES and pid:
                self._kill_process_tree(pid, sig.SIGKILL)

        # Run after_stop hooks
        for task_name in pane_map:
            task_cfg = self.config.tasks[task_name]
            runHook(task_cfg.hooks.after_stop, task_name)

        session_name = self.config.name
        sess.kill()

        # Global after_stop
        runHook(self.config.hooks.after_stop)
        recordEvent("session_stopped", session=session_name)
        return {"ok": True, "session": session_name, "action": "stopped"}

    def restart_all(self) -> dict:
        """Stop all then start all."""
        self.stop_all()
        self._refresh_session()
        return self.start_all()

    def create_session(self) -> dict:
        """Create new tmux session with auto_start tasks only (legacy, wraps start_all)."""
        return self.start_all()

    def restart_task(self, task_name: str) -> dict:
        """Restart a specific task with full stop escalation."""
        self.restart_tracker.clear_manually_stopped(task_name)
        if not self.session_exists():
            return self._err(ErrorCode.SESSION_NOT_FOUND, session=self.config.name)

        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)

        sess = self._get_session()
        task_cfg = self.config.tasks[task_name]
        command = task_cfg.command

        window = sess.windows.get(window_name=task_name, default=None)
        if window:
            # Full stop with signal escalation
            runHook(task_cfg.hooks.before_stop, task_name)
            pane = window.active_pane
            if pane:
                pane.send_keys("C-c")
                if not self._wait_for_exit(pane, timeout=task_cfg.stop_grace_period):
                    pid = self._get_pane_child_pid(pane)
                    if pid:
                        self._kill_process_tree(pid, sig.SIGTERM)
                    if not self._wait_for_exit(pane, timeout=3):
                        if pid:
                            self._kill_process_tree(pid, sig.SIGKILL)
                        self._wait_for_exit(pane, timeout=1)
            runHook(task_cfg.hooks.after_stop, task_name)

            # Port cleanup before restart
            if task_cfg.port:
                self._cleanup_port(task_cfg.port)

            runHook(task_cfg.hooks.before_start, task_name)
            pane = window.active_pane
            if pane:
                if task_cfg.cwd:
                    pane.send_keys(f"cd {task_cfg.cwd}", enter=True)
                pane.send_keys(command, enter=True)
                self._attach_log_pipe(pane, task_name)
            runHook(task_cfg.hooks.after_start, task_name)
        else:
            # Port cleanup before start
            if task_cfg.port:
                self._cleanup_port(task_cfg.port)
            runHook(task_cfg.hooks.before_start, task_name)
            self._send_command_to_window(sess, task_name, command, task_cfg.cwd)
            runHook(task_cfg.hooks.after_start, task_name)

        recordEvent("task_restarted", session=self.config.name, task=task_name)
        return {"ok": True, "task": task_name, "action": "restarted"}

    def kill_task(self, task_name: str) -> dict:
        """Kill a specific task (process group + window)."""
        self.restart_tracker.mark_manually_stopped(task_name)
        if not self.session_exists():
            return self._err(ErrorCode.SESSION_NOT_FOUND, session=self.config.name)

        window = self._get_session().windows.get(window_name=task_name, default=None)
        if not window:
            return self._err(ErrorCode.TASK_NOT_RUNNING, task=task_name)

        pane = window.active_pane
        if pane:
            pid = self._get_pane_child_pid(pane)
            if pid:
                self._kill_process_tree(pid)
        window.kill()
        recordEvent("task_killed", session=self.config.name, task=task_name)
        return {"ok": True, "task": task_name, "action": "killed"}

    def inspect_task(self, task_name: str) -> dict:
        """Return JSON-serializable dict with detailed task state."""
        if task_name not in self.config.tasks:
            return self._err(ErrorCode.TASK_NOT_FOUND, task=task_name)

        task_cfg = self.config.tasks[task_name]
        info: dict = {
            "name": task_name,
            "command": task_cfg.command,
            "auto_start": task_cfg.auto_start,
            "restart_policy": str(task_cfg.restart_policy),
            "log_file": str(_logPath(self.config.name, task_name, task_cfg)),
            "cwd": task_cfg.cwd,
            "health_check": task_cfg.health_check,
            "depends_on": task_cfg.depends_on,
            "running": False,
            "healthy": False,
            "pid": None,
            "pane_current_command": None,
            "pane_current_path": None,
            "window_id": None,
            "pane_id": None,
        }

        if not self.session_exists():
            return info

        sess = self._get_session()
        window = sess.windows.get(window_name=task_name, default=None)
        if not window:
            return info

        info["running"] = True
        info["window_id"] = window.window_id

        pane = window.active_pane
        if pane:
            info["pane_id"] = pane.pane_id
            info["pid"] = getattr(pane, "pane_pid", None)
            info["pane_current_command"] = getattr(pane, "pane_current_command", None)
            info["pane_current_path"] = getattr(pane, "pane_current_path", None)

        result = self.check_health(task_name)
        info["healthy"] = result.ok
        info["last_health"] = result.to_dict()
        return info

    def _tail_panes(
        self,
        panes: list[tuple[str, libtmux.Pane, str]],
        lines: int = 100,
        grep: str | None = None,
    ) -> None:
        """Poll capture-pane and print new lines with colored task prefixes."""
        console = Console()
        state: dict[str, list[str]] = {}

        try:
            while True:
                for task_name, pane, color in panes:
                    output = pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
                    while output and not output[-1].strip():
                        output.pop()

                    prev = state.get(task_name, [])
                    new = _find_new_lines(output, prev)

                    if grep:
                        new = [ln for ln in new if grep.lower() in ln.lower()]

                    for line in new:
                        prefix = escape(f"[{task_name}]")
                        console.print(f"[{color}]{prefix}[/{color}] {escape(line)}")

                    if output:
                        state[task_name] = output[-50:]

                time.sleep(0.5)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped following logs[/dim]")

    def _collect_panes(self, task_names: list[str]) -> list[tuple[str, libtmux.Pane, str]]:
        """Collect (name, pane, color) tuples for running tasks."""
        sess = self._get_session()
        result: list[tuple[str, libtmux.Pane, str]] = []
        for i, name in enumerate(task_names):
            window = sess.windows.get(window_name=name, default=None)
            if not window:
                continue
            pane = window.active_pane
            if pane:
                color = TASK_COLORS[i % len(TASK_COLORS)]
                result.append((name, pane, color))
        return result

    def _read_log_file(
        self, log_path: Path, lines: int, grep: str | None, since: str | None
    ) -> list[str]:
        """Read lines from a persistent log file with optional filtering."""
        try:
            all_lines = log_path.read_text().splitlines()
        except OSError:
            return []

        if since:
            since_dt = _parseSince(since)
            filtered = []
            for line in all_lines:
                # Timestamp is first 23 chars: 2024-01-01T14:00:00.123
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

    def _tail_log_file(self, task_name: str, log_path: Path, grep: str | None, color: str) -> None:
        """Follow a log file (tail -f style)."""
        console = Console()
        try:
            with open(log_path) as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if line:
                        line = line.rstrip("\n")
                        if grep and grep.lower() not in line.lower():
                            continue
                        prefix = escape(f"[{task_name}]")
                        console.print(f"[{color}]{prefix}[/{color}] {escape(line)}")
                    else:
                        time.sleep(0.1)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped following logs[/dim]")

    def _tail_log_files(
        self, task_log_paths: list[tuple[str, Path, str]], grep: str | None
    ) -> None:
        """Follow multiple log files, interleaving output."""
        console = Console()
        handles: list[tuple[str, object, str]] = []
        for task_name, log_path, color in task_log_paths:
            f = open(log_path)  # noqa: SIM115
            f.seek(0, 2)
            handles.append((task_name, f, color))
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
                f.close()  # type: ignore[union-attr]

    def show_logs(
        self,
        task_name: str | None,
        follow: bool = False,
        lines: int = 100,
        grep: str | None = None,
        context: int = 3,
        since: str | None = None,
    ) -> None:
        """Show logs for a task or all tasks."""
        if task_name is None:
            self.show_all_logs(follow=follow, lines=lines, grep=grep, context=context, since=since)
            return

        if task_name not in self.config.tasks:
            raise TaskmuxError(ErrorCode.TASK_NOT_FOUND, task=task_name)

        # Prefer persistent log file
        log_path = self.getLogPath(task_name)
        if log_path:
            if follow:
                color = TASK_COLORS[0]
                self._tail_log_file(task_name, log_path, grep, color)
            else:
                output = self._read_log_file(log_path, lines, grep, since)
                for line in output:
                    print(line)
            return

        # Fallback to capture-pane
        if not self.session_exists():
            raise TaskmuxError(ErrorCode.SESSION_NOT_FOUND, session=self.config.name)

        sess = self._get_session()
        window = sess.windows.get(window_name=task_name, default=None)
        if not window:
            raise TaskmuxError(ErrorCode.TASK_NOT_RUNNING, task=task_name)

        if follow:
            panes = self._collect_panes([task_name])
            if panes:
                self._tail_panes(panes, lines=lines, grep=grep)
        else:
            pane = window.active_pane
            if pane:
                output = pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
                if grep:
                    _print_grep_results(output, grep, context)
                else:
                    for line in output:
                        print(line)

    def show_all_logs(
        self,
        follow: bool = False,
        lines: int = 100,
        grep: str | None = None,
        context: int = 3,
        since: str | None = None,
    ) -> None:
        """Show logs from all running tasks."""
        console = Console()
        task_names = list(self.config.tasks.keys())

        # Try log files first
        if follow:
            log_files: list[tuple[str, Path, str]] = []
            for i, name in enumerate(task_names):
                lp = self.getLogPath(name)
                if lp:
                    log_files.append((name, lp, TASK_COLORS[i % len(TASK_COLORS)]))
            if log_files:
                self._tail_log_files(log_files, grep)
                return
            # Fallback to capture-pane
            if self.session_exists():
                panes = self._collect_panes(task_names)
                if panes:
                    self._tail_panes(panes, lines=lines, grep=grep)
            return

        # Non-follow: prefer log files, fallback to capture-pane per task
        for i, task_name in enumerate(task_names):
            color = TASK_COLORS[i % len(TASK_COLORS)]
            log_path = self.getLogPath(task_name)
            if log_path:
                output = self._read_log_file(log_path, lines, grep, since)
                for line in output:
                    prefix = escape(f"[{task_name}]")
                    console.print(f"[{color}]{prefix}[/{color}] {escape(line)}")
                continue

            if not self.session_exists():
                continue
            sess = self._get_session()
            window = sess.windows.get(window_name=task_name, default=None)
            if not window:
                continue
            pane = window.active_pane
            if not pane:
                continue
            output = pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
            if grep:
                matching = [line for line in output if grep.lower() in line.lower()]
                for line in matching:
                    prefix = escape(f"[{task_name}]")
                    console.print(f"[{color}]{prefix}[/{color}] {escape(line)}")
            else:
                for line in output:
                    prefix = escape(f"[{task_name}]")
                    console.print(f"[{color}]{prefix}[/{color}] {escape(line)}")

    def list_tasks(self) -> dict:
        """List all tasks and their status."""
        exists = self.session_exists()
        windows = self.list_windows() if exists else []

        tasks = []
        for task_name, task_cfg in self.config.tasks.items():
            status = self.get_task_status(task_name)
            last = self.restart_tracker.last_health(task_name)
            tasks.append(
                {
                    "name": task_name,
                    "running": status["running"],
                    "healthy": status["healthy"],
                    "command": task_cfg.command,
                    "auto_start": task_cfg.auto_start,
                    "port": task_cfg.port,
                    "restart_policy": str(task_cfg.restart_policy),
                    "cwd": task_cfg.cwd,
                    "depends_on": task_cfg.depends_on,
                    "last_health": last.to_dict() if last else None,
                }
            )

        return {
            "session": self.config.name,
            "running": exists,
            "active_tasks": len(windows),
            "tasks": tasks,
        }

    def check_task_health(self, task_name: str) -> bool:
        """Check if a task is healthy"""
        is_healthy = self.is_task_healthy(task_name)
        status = self.get_task_status(task_name)

        self.task_health[task_name] = {
            "healthy": is_healthy,
            "last_check": datetime.now(),
            "status": status,
        }

        return is_healthy

    def auto_restart_tasks(self) -> None:
        """Auto-restart tasks based on restart_policy, health_retries, max_restarts, and backoff."""
        if not self.session_exists():
            return

        now = time.time()

        for task_name, task_cfg in self.config.tasks.items():
            if task_cfg.restart_policy == RestartPolicy.NO:
                continue
            if self.restart_tracker.is_manually_stopped(task_name):
                continue

            healthy = self.check_task_health(task_name)
            pane_alive = self._is_pane_alive(task_name)

            if healthy:
                self.restart_tracker.reset_health_failures(task_name)
                # Reset restart tracker after 60s stable
                info = self.restart_tracker.get(task_name)
                if info["count"] > 0 and now - info["last"] > 60:
                    self.restart_tracker.reset(task_name)
                continue

            # "on-failure": restart on crash or health_retries exceeded
            # "always": restart whenever pane is dead (even clean exit)
            should_restart = False

            if not pane_alive:
                # Process exited — restart for both on-failure and always
                should_restart = True
            elif task_cfg.restart_policy == RestartPolicy.ON_FAILURE:
                # Pane alive but health check failing — count consecutive failures
                failures = self.restart_tracker.record_health_failure(task_name)
                recordEvent(
                    "health_check_failed",
                    session=self.config.name,
                    task=task_name,
                    attempt=failures,
                )
                if failures >= task_cfg.health_retries:
                    should_restart = True
            elif task_cfg.restart_policy == RestartPolicy.ALWAYS:
                failures = self.restart_tracker.record_health_failure(task_name)
                recordEvent(
                    "health_check_failed",
                    session=self.config.name,
                    task=task_name,
                    attempt=failures,
                )
                if failures >= task_cfg.health_retries:
                    should_restart = True

            if not should_restart:
                continue

            # Check max_restarts limit
            info = self.restart_tracker.get(task_name)
            if task_cfg.max_restarts and info["count"] >= task_cfg.max_restarts:
                recordEvent(
                    "max_restarts_reached",
                    session=self.config.name,
                    task=task_name,
                    count=int(info["count"]),
                )
                continue

            # Check backoff delay
            delay = min(task_cfg.restart_backoff ** info["count"], 60)
            if info["last"] and now - info["last"] < delay:
                continue

            reason = "process_exited" if not pane_alive else "health_retries_exceeded"
            recordEvent("auto_restart", session=self.config.name, task=task_name, reason=reason)
            print(f"Auto-restarting task: {task_name}")
            self.restart_task(task_name)
            self.restart_tracker.record(task_name)
            self.restart_tracker.reset_health_failures(task_name)

    def auto_restart_unhealthy_tasks(self) -> None:
        """Deprecated: use auto_restart_tasks() instead."""
        self.auto_restart_tasks()

    def stop_session(self) -> dict:
        """Stop the entire tmux session (legacy, wraps stop_all)."""
        return self.stop_all()


def _print_grep_results(output: list[str], pattern: str, context: int) -> None:
    """Print lines matching pattern with surrounding context."""
    matching_indices: list[int] = []
    for i, line in enumerate(output):
        if pattern.lower() in line.lower():
            matching_indices.append(i)

    if not matching_indices:
        print(f"No matches for '{pattern}'")
        return

    # Build set of lines to show
    show: set[int] = set()
    for idx in matching_indices:
        for offset in range(-context, context + 1):
            pos = idx + offset
            if 0 <= pos < len(output):
                show.add(pos)

    last_printed = -2
    for i in sorted(show):
        if i > last_printed + 1:
            print("--")
        marker = ">" if i in matching_indices else " "
        print(f"{marker} {output[i]}")
        last_printed = i
