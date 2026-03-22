"""Typer-based CLI interface for Taskmux."""

import asyncio
import json
from typing import List, Optional  # noqa: UP035

import typer
from rich.console import Console
from rich.table import Table

from .config import addTask, loadConfig, removeTask
from .daemon import SimpleConfigWatcher, TaskmuxDaemon
from .init import initProject
from .models import TaskmuxConfig
from .tmux_manager import TmuxManager

app = typer.Typer(
    name="taskmux",
    help=(
        "Tmux session manager for development environments.\n\n"
        "Reads task definitions from taskmux.toml, manages tmux sessions/windows, "
        "provides health monitoring, restart policies (no/on-failure/always), "
        "dependency ordering, lifecycle hooks, and a WebSocket API.\n\n"
        "Quick start: taskmux init → edit taskmux.toml → taskmux start"
    ),
    epilog="Docs: https://github.com/nc9/taskmux",
    rich_markup_mode="rich",
)

console = Console()


class TaskmuxCLI:
    """Main CLI application class."""

    def __init__(self):
        self.config: TaskmuxConfig = loadConfig()
        self.tmux = TmuxManager(self.config)

    def handle_config_reload(self):
        """Handle config file reload in daemon mode"""
        current_windows = self.tmux.list_windows()

        for task_name, _task_cfg in self.config.tasks.items():
            if task_name in current_windows:
                console.print(f"Reloading task '{task_name}' due to config change")
                self.tmux.restart_task(task_name)
            else:
                if self.tmux.session_exists():
                    console.print(f"Adding new task '{task_name}'")
                    self.tmux.restart_task(task_name)


@app.command()
def init(
    defaults: bool = typer.Option(False, "--defaults", help="Accept all defaults"),
):
    """Initialize taskmux config in current directory.

    Creates taskmux.toml with session name (defaults to directory name).
    Detects installed AI coding agents (Claude, Codex, OpenCode) and injects
    taskmux usage instructions into their context files.
    Use --defaults to skip interactive prompts.
    """
    initProject(defaults=defaults)


@app.command()
def start(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
    monitor: bool = typer.Option(  # noqa: B008
        False, "-m", "--monitor", help="Stay running, auto-restart per restart_policy"
    ),
):
    """Start tasks (all auto_start tasks if none specified).

    Starts tasks in dependency order, waiting for each dependency's health check
    to pass before starting dependents. With --monitor, stays in the foreground
    and auto-restarts tasks according to their restart_policy (no/on-failure/always),
    respecting health_retries, max_restarts, and exponential backoff.
    """
    import time

    cli = TaskmuxCLI()
    if tasks:
        for task in tasks:
            cli.tmux.start_task(task)
    else:
        cli.tmux.start_all()

    if monitor:
        console.print("Monitoring tasks (Ctrl+C to stop)...")
        try:
            while True:
                time.sleep(30)
                cli.tmux.auto_restart_tasks()
        except KeyboardInterrupt:
            console.print("\nStopped monitoring")


@app.command()
def stop(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
):
    """Stop tasks (all if none specified).

    Uses signal escalation: C-c → SIGTERM → SIGKILL. Waits stop_grace_period
    seconds (default 5) after C-c before escalating. Stopped tasks are marked
    as manually stopped and will not be auto-restarted even with restart_policy="always".
    """
    cli = TaskmuxCLI()
    if tasks:
        for task in tasks:
            cli.tmux.stop_task(task)
    else:
        cli.tmux.stop_all()


@app.command()
def restart(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
):
    """Restart tasks (all if none specified).

    Full stop with signal escalation, port cleanup, then restart.
    Clears the manually-stopped flag so auto-restart policies resume.
    """
    cli = TaskmuxCLI()
    if tasks:
        for task in tasks:
            cli.tmux.restart_task(task)
    else:
        cli.tmux.restart_all()


@app.command()
def kill(
    task: str = typer.Argument(..., help="Task name to kill"),
):
    """Kill a specific task (SIGKILL + destroy window).

    Unlike stop, kill is immediate with no grace period. The tmux window is
    destroyed. The task is marked as manually stopped (no auto-restart).
    """
    cli = TaskmuxCLI()
    cli.tmux.kill_task(task)


@app.command()
def logs(
    task: str | None = typer.Argument(None, help="Task name (omit for all)"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow logs"),
    lines: int = typer.Option(100, "-n", "--lines", help="Number of lines"),
    grep: str | None = typer.Option(None, "-g", "--grep", help="Filter logs by pattern"),
    context: int = typer.Option(3, "-C", "--context", help="Context lines around grep matches"),
):
    """Show logs for a task, or interleaved logs from all tasks.

    Without -f, prints recent output. With -f, follows logs live with colored
    task prefixes. Use -g to grep across tasks and -C for context lines.
    """
    cli = TaskmuxCLI()
    cli.tmux.show_logs(task, follow, lines, grep=grep, context=context)


@app.command()
def inspect(
    task: str = typer.Argument(..., help="Task name to inspect"),
):
    """Inspect task state as JSON.

    Returns detailed info: name, command, restart_policy, running/healthy status,
    pid, pane command, cwd, window/pane IDs, health_check, and depends_on.
    """
    cli = TaskmuxCLI()
    data = cli.tmux.inspect_task(task)
    console.print_json(json.dumps(data))


@app.command()
def add(
    task: str = typer.Argument(..., help="Task name"),
    command: str = typer.Argument(..., help="Command to run"),
    cwd: str | None = typer.Option(None, "--cwd", help="Working directory"),
    health_check: str | None = typer.Option(None, "--health-check", help="Health check command"),
    depends_on: Optional[List[str]] = typer.Option(  # noqa: UP006, UP045, B008
        None, "--depends-on", help="Dependency task names"
    ),
):
    """Add a new task to taskmux.toml."""
    addTask(None, task, command, cwd=cwd, health_check=health_check, depends_on=depends_on)
    console.print(f"Added task '{task}': {command}")


@app.command()
def remove(
    task: str = typer.Argument(..., help="Task name to remove"),
):
    """Remove a task from taskmux.toml (kills it first if running)."""
    cli = TaskmuxCLI()

    if cli.tmux.session_exists():
        cli.tmux.kill_task(task)

    _, removed = removeTask(None, task)
    if removed:
        console.print(f"Removed task '{task}'")
    else:
        console.print(f"Task '{task}' not found in config", style="red")


def _status():
    """Show session and task status.

    Lists all tasks with health indicators, running state, ports, restart policy
    (if non-default), working directory, and dependencies. Aliases: list, ls.
    """
    cli = TaskmuxCLI()
    cli.tmux.list_tasks()


app.command(name="status")(_status)
app.command(name="list", hidden=True)(_status)
app.command(name="ls", hidden=True)(_status)


@app.command()
def health():
    """Check health of all tasks.

    Runs each task's health_check command (or falls back to pane-alive check).
    Displays a table with health status for every configured task.
    """
    cli = TaskmuxCLI()

    if not cli.tmux.session_exists():
        console.print("No session running", style="yellow")
        return

    table = Table(title="Health Check Results")
    table.add_column("Status", style="cyan")
    table.add_column("Task", style="magenta")
    table.add_column("Health", style="green")

    healthy_count = 0
    total_count = len(cli.config.tasks)

    for task_name in cli.config.tasks:
        is_healthy = cli.tmux.check_task_health(task_name)
        status_icon = "G" if is_healthy else "R"
        status_text = "Healthy" if is_healthy else "Unhealthy"

        table.add_row(status_icon, task_name, status_text)

        if is_healthy:
            healthy_count += 1

    console.print(table)
    console.print(f"Health: {healthy_count}/{total_count} tasks healthy")


@app.command()
def watch():
    """Watch taskmux.toml for changes and reload on edit.

    Stays in the foreground. When the config file changes, reloads it and
    restarts affected tasks.
    """
    cli = TaskmuxCLI()
    watcher = SimpleConfigWatcher(cli)
    watcher.watch_config()


@app.command()
def daemon(
    port: int = typer.Option(8765, "--port", help="WebSocket API port"),
):
    """Run in daemon mode with WebSocket API and health monitoring.

    Monitors task health every 30s and auto-restarts per restart_policy with
    exponential backoff. Watches config for changes. Exposes a WebSocket API
    for status, restart, kill, and logs commands.
    """
    d = TaskmuxDaemon(api_port=port)
    asyncio.run(d.start())


def main():
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
