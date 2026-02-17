"""Tmux session and task management."""

from __future__ import annotations

import time
from datetime import datetime

import libtmux

from .hooks import runHook
from .models import TaskmuxConfig


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

        if self.session and status["running"]:
            try:
                window = self._get_session().windows.get(window_name=task_name, default=None)
                if window and window.active_pane:
                    current_command = getattr(window.active_pane, "pane_current_command", "")
                    status["healthy"] = current_command != "" and current_command != "bash"
            except Exception:
                pass

        return status

    def _send_command_to_window(
        self, sess: libtmux.Session, task_name: str, command: str
    ) -> libtmux.Window:
        """Create a new window and send a command to it."""
        window = sess.new_window(attach=False, window_name=task_name)
        pane = window.active_pane
        if pane:
            pane.send_keys(command, enter=True)
        return window

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
                    pane.send_keys(task_cfg.command, enter=True)
            else:
                self._send_command_to_window(sess, task_name, task_cfg.command)
        else:
            self._send_command_to_window(sess, task_name, task_cfg.command)

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
        """Start all auto_start tasks (or create session if global auto_start=False)."""
        if self.session_exists():
            print(f"Session '{self.config.name}' already exists")
            return

        if not self.config.auto_start:
            # Create empty session, no tasks
            self.session = self.server.new_session(session_name=self.config.name, attach=False)
            print(f"Created session '{self.config.name}' (auto_start disabled, no tasks launched)")
            return

        auto_tasks = [(name, cfg) for name, cfg in self.config.tasks.items() if cfg.auto_start]
        if not auto_tasks:
            print("No auto-start tasks defined in config")
            return

        # Global before_start
        if not runHook(self.config.hooks.before_start):
            return

        self.session = self.server.new_session(session_name=self.config.name, attach=False)
        sess = self._get_session()

        # First task reuses default window
        first_name, first_cfg = auto_tasks[0]
        runHook(first_cfg.hooks.before_start, first_name)
        if sess.windows:
            default_window = sess.windows[0]
            default_window.rename_window(first_name)
            pane = default_window.active_pane
            if pane:
                pane.send_keys(first_cfg.command, enter=True)
        runHook(first_cfg.hooks.after_start, first_name)

        for task_name, task_cfg in auto_tasks[1:]:
            runHook(task_cfg.hooks.before_start, task_name)
            self._send_command_to_window(sess, task_name, task_cfg.command)
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
                pane.send_keys(command, enter=True)
            runHook(task_cfg.hooks.after_start, task_name)
        else:
            runHook(task_cfg.hooks.before_start, task_name)
            self._send_command_to_window(sess, task_name, command)
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

            current_cmd = info["pane_current_command"] or ""
            info["healthy"] = current_cmd != "" and current_cmd != "bash"

        return info

    def show_logs(
        self,
        task_name: str,
        follow: bool = False,
        lines: int = 100,
        grep: str | None = None,
        context: int = 3,
    ) -> None:
        """Show logs for a task, optionally filtering with grep."""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist")
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
            window.select_window()
            sess.attach()
        else:
            pane = window.active_pane
            if pane:
                output = pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
                if grep:
                    _print_grep_results(output, grep, context)
                else:
                    for line in output:
                        print(line)

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
            print(f"{health_icon} {status_text:8} {task_name:15} {task_cfg.command}{auto}")

    def show_status(self) -> None:
        """Show overall session status"""
        exists = self.session_exists()
        print(f"Session '{self.config.name}': {'Running' if exists else 'Stopped'} (libtmux)")

        if exists:
            windows = self.list_windows()
            print(f"Active tasks: {len(windows)}")
            self.list_tasks()

    def check_task_health(self, task_name: str) -> bool:
        """Check if a task is healthy (process still running)"""
        status = self.get_task_status(task_name)
        is_healthy = bool(status["running"] and status["healthy"])

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
