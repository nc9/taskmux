"""Typer-based CLI interface for Taskmux."""

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from .config import addTask, loadConfig, removeTask
from .daemon import SimpleConfigWatcher, TaskmuxDaemon
from .models import TaskmuxConfig
from .tmux_manager import TmuxManager

app = typer.Typer(
    name="taskmux",
    help="Modern tmux development environment manager with health monitoring and auto-restart",
    epilog="Uses libtmux API with health monitoring and daemon capabilities.",
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
def list():
    """List all tasks and their status."""
    cli = TaskmuxCLI()
    cli.tmux.list_tasks()


@app.command()
def start():
    """Start all tasks."""
    cli = TaskmuxCLI()
    cli.tmux.create_session()


@app.command()
def restart(
    task: str = typer.Argument(..., help="Task name to restart"),
):
    """Restart a specific task."""
    cli = TaskmuxCLI()
    cli.tmux.restart_task(task)


@app.command()
def kill(
    task: str = typer.Argument(..., help="Task name to kill"),
):
    """Kill a specific task."""
    cli = TaskmuxCLI()
    cli.tmux.kill_task(task)


@app.command()
def logs(
    task: str = typer.Argument(..., help="Task name"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow logs"),
    lines: int = typer.Option(100, "-n", "--lines", help="Number of lines"),
):
    """Show logs for a task."""
    cli = TaskmuxCLI()
    cli.tmux.show_logs(task, follow, lines)


@app.command()
def add(
    task: str = typer.Argument(..., help="Task name"),
    command: str = typer.Argument(..., help="Command to run"),
):
    """Add a new task."""
    addTask(None, task, command)
    console.print(f"Added task '{task}': {command}")


@app.command()
def remove(
    task: str = typer.Argument(..., help="Task name to remove"),
):
    """Remove a task."""
    cli = TaskmuxCLI()

    if cli.tmux.session_exists():
        cli.tmux.kill_task(task)

    _, removed = removeTask(None, task)
    if removed:
        console.print(f"Removed task '{task}'")
    else:
        console.print(f"Task '{task}' not found in config", style="red")


@app.command()
def status():
    """Show session status."""
    cli = TaskmuxCLI()
    cli.tmux.show_status()


@app.command()
def health():
    """Check health of all tasks."""
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
    """Watch config file for changes."""
    cli = TaskmuxCLI()
    watcher = SimpleConfigWatcher(cli)
    watcher.watch_config()


@app.command()
def daemon(
    port: int = typer.Option(8765, "--port", help="WebSocket API port"),
):
    """Run in daemon mode with API."""
    d = TaskmuxDaemon(api_port=port)
    asyncio.run(d.start())


@app.command()
def stop():
    """Stop the session and all tasks."""
    cli = TaskmuxCLI()
    cli.tmux.stop_session()


def main():
    """Main entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
