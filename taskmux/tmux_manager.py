"""Tmux session and task management."""

from __future__ import annotations

import time
from datetime import datetime

import libtmux

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
                window = self._get_session().windows.get(window_name=task_name)
                if window and window.active_pane:
                    current_command = getattr(window.active_pane, "pane_current_command", "")
                    status["healthy"] = current_command != "" and current_command != "bash"
            except Exception:
                pass

        return status

    def create_session(self) -> None:
        """Create new tmux session with auto_start tasks only"""
        if self.session_exists():
            print(f"Session '{self.config.name}' already exists")
            return

        auto_tasks = [(name, cfg) for name, cfg in self.config.tasks.items() if cfg.auto_start]
        if not auto_tasks:
            print("No auto-start tasks defined in config")
            return

        self.session = self.server.new_session(session_name=self.config.name, attach=False)
        sess = self._get_session()

        first_name, first_cfg = auto_tasks[0]
        if sess.windows:
            default_window = sess.windows[0]
            default_window.rename_window(first_name)
            pane = default_window.active_pane
            if pane:
                pane.send_keys(first_cfg.command, enter=True)

        for task_name, task_cfg in auto_tasks[1:]:
            window = sess.new_window(attach=False, window_name=task_name)
            pane = window.active_pane
            if pane:
                pane.send_keys(task_cfg.command, enter=True)

        print(f"Started session '{self.config.name}' with {len(auto_tasks)} tasks")

    def restart_task(self, task_name: str) -> None:
        """Restart a specific task (works regardless of auto_start)"""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist. Run 'taskmux start' first.")
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        sess = self._get_session()
        command = self.config.tasks[task_name].command

        window = sess.windows.get(window_name=task_name)
        if window:
            pane = window.active_pane
            if pane:
                pane.send_keys("C-c")
                time.sleep(0.5)
                pane.send_keys(command, enter=True)
        else:
            window = sess.new_window(attach=False, window_name=task_name)
            pane = window.active_pane
            if pane:
                pane.send_keys(command, enter=True)

        print(f"Restarted task '{task_name}'")

    def kill_task(self, task_name: str) -> None:
        """Kill a specific task"""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist")
            return

        window = self._get_session().windows.get(window_name=task_name)
        if window:
            window.kill()
            print(f"Killed task '{task_name}'")
        else:
            print(f"Task '{task_name}' not found")

    def show_logs(self, task_name: str, follow: bool = False, lines: int = 100) -> None:
        """Show logs for a task"""
        if not self.session_exists():
            print(f"Session '{self.config.name}' doesn't exist")
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        sess = self._get_session()
        window = sess.windows.get(window_name=task_name)
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
        """Stop the entire tmux session"""
        if not self.session_exists():
            print("No session running")
            return

        self._get_session().kill()
        print(f"Stopped session '{self.config.name}'")
