"""
Tmux session and task management.
"""

import time
from datetime import datetime
from typing import Dict, List, Union

import libtmux

from .config import TaskmuxConfig


class TmuxManager:
    """Manages tmux sessions and tasks using libtmux API."""

    def __init__(self, config: TaskmuxConfig):
        self.config = config
        self.server = libtmux.Server()
        self.session = None
        self.task_health = {}  # Track task health status
        self._refresh_session()

    def _refresh_session(self):
        """Refresh session object from server"""
        try:
            self.session = self.server.sessions.get(
                session_name=self.config.session_name
            )
        except Exception:
            self.session = None

    def session_exists(self) -> bool:
        """Check if tmux session exists"""
        self._refresh_session()
        return self.session is not None

    def list_windows(self) -> List[str]:
        """List all windows in the session"""
        if not self.session_exists():
            return []

        try:
            return [window.window_name for window in self.session.windows]
        except Exception:
            return []

    def get_task_status(self, task_name: str) -> Dict[str, Union[str, bool]]:
        """Get detailed status for a task"""
        status = {
            "name": task_name,
            "running": False,
            "healthy": False,
            "command": self.config.tasks.get(task_name, ""),
            "last_check": datetime.now().isoformat(),
        }

        if not self.session_exists():
            return status

        windows = self.list_windows()
        status["running"] = task_name in windows

        if self.session and status["running"]:
            try:
                window = self.session.windows.get(window_name=task_name)
                if window:
                    # Check if process is still running
                    pane = window.active_pane
                    current_command = getattr(pane, "pane_current_command", "")
                    status["healthy"] = (
                        current_command != "" and current_command != "bash"
                    )
            except Exception:
                pass

        return status

    def create_session(self):
        """Create new tmux session with all tasks"""
        if self.session_exists():
            print(f"Session '{self.config.session_name}' already exists")
            return

        tasks = list(self.config.tasks.items())
        if not tasks:
            print("No tasks defined in config")
            return

        # Create session with libtmux
        self.session = self.server.new_session(
            session_name=self.config.session_name, attach=False
        )

        # Rename the default window to first task, then create remaining tasks
        first_task, first_command = tasks[0]
        if self.session.windows:
            # Rename default window to first task
            default_window = self.session.windows[0]
            default_window.rename_window(first_task)
            # Send command to the window
            pane = default_window.active_pane
            pane.send_keys(first_command, enter=True)

        # Create windows for remaining tasks
        for task_name, command in tasks[1:]:
            window = self.session.new_window(
                attach=False, window_name=task_name
            )
            # Send command to the window
            pane = window.active_pane
            pane.send_keys(command, enter=True)

        print(
            f"✓ Created session '{self.config.session_name}' with {len(tasks)} tasks"
        )

    def restart_task(self, task_name: str):
        """Restart a specific task"""
        if not self.session_exists():
            print(
                f"Session '{self.config.session_name}' doesn't exist. Run 'taskmux start' first."
            )
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        command = self.config.tasks[task_name]

        window = self.session.windows.get(window_name=task_name)
        if window:
            # Kill current process and restart
            pane = window.active_pane
            pane.send_keys("C-c")
            time.sleep(0.5)
            pane.send_keys(command, enter=True)
        else:
            # Create new window
            window = self.session.new_window(
                attach=False, window_name=task_name
            )
            pane = window.active_pane
            pane.send_keys(command, enter=True)

        print(f"✓ Restarted task '{task_name}'")

    def kill_task(self, task_name: str):
        """Kill a specific task"""
        if not self.session_exists():
            print(f"Session '{self.config.session_name}' doesn't exist")
            return

        window = self.session.windows.get(window_name=task_name)
        if window:
            window.kill()
            print(f"✓ Killed task '{task_name}'")
        else:
            print(f"Task '{task_name}' not found")

    def show_logs(self, task_name: str, follow: bool = False, lines: int = 100):
        """Show logs for a task"""
        if not self.session_exists():
            print(f"Session '{self.config.session_name}' doesn't exist")
            return

        if task_name not in self.config.tasks:
            print(f"Task '{task_name}' not found in config")
            return

        window = self.session.windows.get(window_name=task_name)
        if not window:
            print(f"Task '{task_name}' not found")
            return

        if follow:
            # Attach to the window to follow logs
            window.select_window()
            self.session.attach()
        else:
            # Show recent logs
            pane = window.active_pane
            output = pane.cmd(
                "capture-pane", "-p", "-S", f"-{lines}"
            ).stdout
            for line in output:
                print(line)

    def list_tasks(self):
        """List all tasks and their status"""
        print(f"Session: {self.config.session_name}")
        print("─" * 70)

        if not self.config.tasks:
            print("No tasks configured")
            return

        for task_name, command in self.config.tasks.items():
            status = self.get_task_status(task_name)
            health_icon = (
                "💚" if status["healthy"] else "🔴" if status["running"] else "○"
            )
            status_text = (
                "Healthy"
                if status["healthy"]
                else "Running"
                if status["running"]
                else "Stopped"
            )
            print(f"{health_icon} {status_text:8} {task_name:15} {command}")

    def show_status(self):
        """Show overall session status"""
        exists = self.session_exists()
        print(
            f"Session '{self.config.session_name}': {'Running' if exists else 'Stopped'} (libtmux)"
        )

        if exists:
            windows = self.list_windows()
            print(f"Active tasks: {len(windows)}")
            self.list_tasks()

    def check_task_health(self, task_name: str) -> bool:
        """Check if a task is healthy (process still running)"""
        status = self.get_task_status(task_name)
        is_healthy = status["running"] and status["healthy"]

        # Update health tracking
        self.task_health[task_name] = {
            "healthy": is_healthy,
            "last_check": datetime.now(),
            "status": status,
        }

        return is_healthy

    def auto_restart_unhealthy_tasks(self):
        """Auto-restart tasks that have become unhealthy"""
        if not self.session_exists():
            return

        for task_name in self.config.tasks:
            if not self.check_task_health(task_name):
                # Check if it was previously healthy (avoid restart loops)
                prev_health = self.task_health.get(task_name, {}).get("healthy", True)
                if prev_health:  # Only restart if it was previously healthy
                    print(f"🔄 Auto-restarting unhealthy task: {task_name}")
                    self.restart_task(task_name)

    def stop_session(self):
        """Stop the entire tmux session"""
        if not self.session_exists():
            print("No session running")
            return

        self.session.kill()
        print(f"✓ Stopped session '{self.config.session_name}'")