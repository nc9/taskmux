"""Tmux session and task management."""

from __future__ import annotations

import subprocess
import time
from collections import deque
from datetime import datetime

import libtmux
from rich.console import Console
from rich.markup import escape

from .hooks import runHook
from .models import TaskmuxConfig

TASK_COLORS = ["cyan", "green", "yellow", "magenta", "blue", "red"]


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
                return cmd != "" and cmd != "bash"
        except Exception:
            pass
        return False

    def is_task_healthy(self, task_name: str) -> bool:
        """Check task health. Uses health_check command if configured, falls back to pane-alive."""
        task_cfg = self.config.tasks.get(task_name)
        if not task_cfg:
            return False

        if not task_cfg.health_check:
            return self._is_pane_alive(task_name)

        try:
            result = subprocess.run(
                task_cfg.health_check,
                shell=True,
                capture_output=True,
                timeout=task_cfg.health_timeout,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

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
            raise ValueError("Dependency cycle detected in tasks")

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

    def start_task(self, task_name: str) -> None:
        """Start a single task (create window + send command)."""
        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        if not self.session_exists():
            # Create empty session first
            self.session = self.server.new_session(session_name=self.config.name, attach=False)

        sess = self._get_session()
        task_cfg = self.config.tasks[task_name]

        # Check if already running
        existing = sess.windows.get(window_name=task_name, default=None)
        if existing:
            print(f"Task '{task_name}' already running")
            return

        # Warn if deps aren't running
        for dep in task_cfg.depends_on:
            if dep not in self.list_windows():
                print(f"Warning: dependency '{dep}' is not running")

        # Hooks: global before_start, then task before_start
        if not runHook(self.config.hooks.before_start, task_name):
            return
        if not runHook(task_cfg.hooks.before_start, task_name):
            return

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
            else:
                self._send_command_to_window(sess, task_name, task_cfg.command, task_cfg.cwd)
        else:
            self._send_command_to_window(sess, task_name, task_cfg.command, task_cfg.cwd)

        runHook(task_cfg.hooks.after_start, task_name)
        runHook(self.config.hooks.after_start, task_name)
        print(f"Started task '{task_name}'")

    def stop_task(self, task_name: str) -> None:
        """Graceful stop (C-c) a single task. Window stays alive."""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist")
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        sess = self._get_session()
        window = sess.windows.get(window_name=task_name, default=None)
        if not window:
            print(f"Task '{task_name}' not running")
            return

        task_cfg = self.config.tasks[task_name]

        # Hooks: global before_stop, then task before_stop
        runHook(self.config.hooks.before_stop, task_name)
        runHook(task_cfg.hooks.before_stop, task_name)

        pane = window.active_pane
        if pane:
            pane.send_keys("C-c")

        # Hooks: task after_stop, then global after_stop
        runHook(task_cfg.hooks.after_stop, task_name)
        runHook(self.config.hooks.after_stop, task_name)
        print(f"Stopped task '{task_name}'")

    def start_all(self) -> None:
        """Start all auto_start tasks in dependency order."""
        if self.session_exists():
            print(f"Session '{self.config.name}' already exists")
            return

        if not self.config.auto_start:
            # Create empty session, no tasks
            self.session = self.server.new_session(session_name=self.config.name, attach=False)
            print(f"Created session '{self.config.name}' (auto_start disabled, no tasks launched)")
            return

        auto_tasks = {name: cfg for name, cfg in self.config.tasks.items() if cfg.auto_start}
        if not auto_tasks:
            print("No auto-start tasks defined in config")
            return

        # Topological sort for dependency ordering
        sorted_names = self._toposort_tasks(list(auto_tasks.keys()))

        # Global before_start
        if not runHook(self.config.hooks.before_start):
            return

        self.session = self.server.new_session(session_name=self.config.name, attach=False)
        sess = self._get_session()

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
                        print(f"Warning: dependency '{dep}' not healthy, skipping '{task_name}'")
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
                first = False
            else:
                self._send_command_to_window(sess, task_name, task_cfg.command, task_cfg.cwd)

            runHook(task_cfg.hooks.after_start, task_name)

        # Global after_start
        runHook(self.config.hooks.after_start)
        print(f"Started session '{self.config.name}' with {len(auto_tasks)} tasks")

    def stop_all(self) -> None:
        """Stop all tasks then kill session."""
        if not self.session_exists():
            print("No session running")
            return

        # Global before_stop
        runHook(self.config.hooks.before_stop)

        # Stop each task with hooks
        sess = self._get_session()
        for task_name, task_cfg in self.config.tasks.items():
            window = sess.windows.get(window_name=task_name, default=None)
            if window:
                runHook(task_cfg.hooks.before_stop, task_name)
                pane = window.active_pane
                if pane:
                    pane.send_keys("C-c")
                runHook(task_cfg.hooks.after_stop, task_name)

        sess.kill()

        # Global after_stop
        runHook(self.config.hooks.after_stop)
        print(f"Stopped session '{self.config.name}'")

    def restart_all(self) -> None:
        """Stop all then start all."""
        self.stop_all()
        self._refresh_session()
        self.start_all()

    def create_session(self) -> None:
        """Create new tmux session with auto_start tasks only (legacy, wraps start_all)."""
        self.start_all()

    def restart_task(self, task_name: str) -> None:
        """Restart a specific task (works regardless of auto_start)"""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist. Run 'taskmux start' first.")
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        sess = self._get_session()
        task_cfg = self.config.tasks[task_name]
        command = task_cfg.command

        window = sess.windows.get(window_name=task_name, default=None)
        if window:
            runHook(task_cfg.hooks.before_stop, task_name)
            pane = window.active_pane
            if pane:
                pane.send_keys("C-c")
                time.sleep(0.5)
            runHook(task_cfg.hooks.after_stop, task_name)

            runHook(task_cfg.hooks.before_start, task_name)
            pane = window.active_pane
            if pane:
                if task_cfg.cwd:
                    pane.send_keys(f"cd {task_cfg.cwd}", enter=True)
                pane.send_keys(command, enter=True)
            runHook(task_cfg.hooks.after_start, task_name)
        else:
            runHook(task_cfg.hooks.before_start, task_name)
            self._send_command_to_window(sess, task_name, command, task_cfg.cwd)
            runHook(task_cfg.hooks.after_start, task_name)

        print(f"Restarted task '{task_name}'")

    def kill_task(self, task_name: str) -> None:
        """Kill a specific task"""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist")
            return

        window = self._get_session().windows.get(window_name=task_name, default=None)
        if window:
            window.kill()
            print(f"Killed task '{task_name}'")
        else:
            print(f"Task '{task_name}' not found")

    def inspect_task(self, task_name: str) -> dict:
        """Return JSON-serializable dict with detailed task state."""
        if task_name not in self.config.tasks:
            return {"error": f"Task '{task_name}' not found in config"}

        task_cfg = self.config.tasks[task_name]
        info: dict = {
            "name": task_name,
            "command": task_cfg.command,
            "auto_start": task_cfg.auto_start,
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

        info["healthy"] = self.is_task_healthy(task_name)
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

    def show_logs(
        self,
        task_name: str | None,
        follow: bool = False,
        lines: int = 100,
        grep: str | None = None,
        context: int = 3,
    ) -> None:
        """Show logs for a task or all tasks."""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist")
            return

        if task_name is None:
            self.show_all_logs(follow=follow, lines=lines, grep=grep, context=context)
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        sess = self._get_session()
        window = sess.windows.get(window_name=task_name, default=None)
        if not window:
            print(f"Task '{task_name}' not found")
            return

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
    ) -> None:
        """Show logs from all running tasks."""
        sess = self._get_session()
        console = Console()
        task_names = list(self.config.tasks.keys())

        if follow:
            panes = self._collect_panes(task_names)
            if panes:
                self._tail_panes(panes, lines=lines, grep=grep)
            return

        for i, task_name in enumerate(task_names):
            window = sess.windows.get(window_name=task_name, default=None)
            if not window:
                continue
            pane = window.active_pane
            if not pane:
                continue
            color = TASK_COLORS[i % len(TASK_COLORS)]
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

    def list_tasks(self) -> None:
        """List all tasks and their status"""
        print(f"Session: {self.config.name}")
        print("-" * 70)

        if not self.config.tasks:
            print("No tasks configured")
            return

        for task_name, task_cfg in self.config.tasks.items():
            status = self.get_task_status(task_name)
            health_icon = "G" if status["healthy"] else "R" if status["running"] else "o"
            status_text = (
                "Healthy" if status["healthy"] else "Running" if status["running"] else "Stopped"
            )
            auto = "" if task_cfg.auto_start else " [manual]"
            extras = ""
            if task_cfg.cwd:
                extras += f" cwd={task_cfg.cwd}"
            if task_cfg.depends_on:
                extras += f" deps=[{','.join(task_cfg.depends_on)}]"
            print(f"{health_icon} {status_text:8} {task_name:15} {task_cfg.command}{auto}{extras}")

    def show_status(self) -> None:
        """Show overall session status"""
        exists = self.session_exists()
        print(f"Session '{self.config.name}': {'Running' if exists else 'Stopped'} (libtmux)")

        if exists:
            windows = self.list_windows()
            print(f"Active tasks: {len(windows)}")
            self.list_tasks()

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

    def auto_restart_unhealthy_tasks(self) -> None:
        """Auto-restart tasks that have become unhealthy"""
        if not self.session_exists():
            return

        for task_name in self.config.tasks:
            if not self.check_task_health(task_name):
                prev_health = self.task_health.get(task_name, {}).get("healthy", True)
                if prev_health:
                    print(f"Auto-restarting unhealthy task: {task_name}")
                    self.restart_task(task_name)

    def stop_session(self) -> None:
        """Stop the entire tmux session (legacy, wraps stop_all)."""
        self.stop_all()


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
