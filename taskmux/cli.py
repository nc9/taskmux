"""Typer-based CLI interface for Taskmux."""

import asyncio
import json
import sys
from typing import List, Optional  # noqa: UP035

import typer
from rich.console import Console
from rich.table import Table

from .config import addTask, loadConfig, removeTask
from .daemon import SimpleConfigWatcher, TaskmuxDaemon
from .errors import TaskmuxError
from .init import initProject
from .models import TaskmuxConfig
from .output import is_json_mode, print_error, print_result, set_json_mode
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
    no_args_is_help=True,
)

console = Console()


def _print_result_human(result: dict) -> None:
    """Print a TmuxManager result dict in human-readable format."""
    if not result.get("ok"):
        code = result.get("error_code", "")
        msg = result.get("error", "Unknown error")
        prefix = f"[{code}] " if code else ""
        console.print(f"Error: {prefix}{msg}", style="red")
        return
    action = result.get("action", "")
    if "task" in result:
        console.print(f"{action.title()} task '{result['task']}'")
    elif "session" in result:
        tasks = result.get("tasks", [])
        msg = f"{action.title()} session '{result['session']}'"
        if tasks:
            msg += f" with {len(tasks)} tasks"
        console.print(msg)
    for w in result.get("warnings", []):
        console.print(f"  Warning: {w}", style="yellow")


def _handle_result(result: dict) -> None:
    """Output result as JSON or human-readable."""
    if is_json_mode():
        print_result(result)
    else:
        _print_result_human(result)


def _handle_results(results: list[dict]) -> None:
    """Output multiple results."""
    if is_json_mode():
        print_result({"ok": all(r.get("ok") for r in results), "results": results})
    else:
        for r in results:
            _print_result_human(r)


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
                self.tmux.restart_task(task_name)
            else:
                if self.tmux.session_exists():
                    self.tmux.restart_task(task_name)


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version

        typer.echo(f"taskmux {version('taskmux')}")
        raise typer.Exit()


@app.callback()
def main_callback(
    json_output: bool = typer.Option(  # noqa: B008
        False, "--json", help="Output as JSON for programmatic use"
    ),
    version: bool = typer.Option(  # noqa: B008
        False,
        "--version",
        "-V",
        help="Show version and exit",
        callback=_version_callback,
        is_eager=True,
    ),
):
    """Taskmux CLI."""
    set_json_mode(json_output)


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
    result = initProject(defaults=defaults)
    if is_json_mode():
        print_result(
            {
                "ok": True,
                "session": result.name,
                "config_path": "taskmux.toml",
            }
        )


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
        results = [cli.tmux.start_task(t) for t in tasks]
        _handle_results(results)
    else:
        result = cli.tmux.start_all()
        _handle_result(result)

    if monitor:
        if not is_json_mode():
            console.print("Monitoring tasks (Ctrl+C to stop)...")
        try:
            while True:
                time.sleep(30)
                cli.tmux.auto_restart_tasks()
        except KeyboardInterrupt:
            if not is_json_mode():
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
        results = [cli.tmux.stop_task(t) for t in tasks]
        _handle_results(results)
    else:
        _handle_result(cli.tmux.stop_all())


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
        results = [cli.tmux.restart_task(t) for t in tasks]
        _handle_results(results)
    else:
        _handle_result(cli.tmux.restart_all())


@app.command()
def kill(
    task: str = typer.Argument(..., help="Task name to kill"),
):
    """Kill a specific task (SIGKILL + destroy window).

    Unlike stop, kill is immediate with no grace period. The tmux window is
    destroyed. The task is marked as manually stopped (no auto-restart).
    """
    cli = TaskmuxCLI()
    _handle_result(cli.tmux.kill_task(task))


@app.command()
def logs(
    task: str | None = typer.Argument(None, help="Task name (omit for all)"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow logs"),
    lines: int = typer.Option(100, "-n", "--lines", help="Number of lines"),
    grep: str | None = typer.Option(None, "-g", "--grep", help="Filter logs by pattern"),
    context: int = typer.Option(3, "-C", "--context", help="Context lines around grep matches"),
    since: str | None = typer.Option(  # noqa: B008
        None, "--since", help="Show logs since time (e.g. '5m', '1h', '2d', or ISO timestamp)"
    ),
):
    """Show logs for a task, or interleaved logs from all tasks.

    Reads from persistent log files when available (with timestamps, survives
    session kill). Falls back to tmux scrollback. Use --since to filter by time,
    -g to grep, -f to follow live. Logs are stored at ~/.taskmux/logs/.
    """
    cli = TaskmuxCLI()
    if is_json_mode() and not follow:
        # Return logs as JSON

        if task:
            log_path = cli.tmux.getLogPath(task)
            if log_path:
                output = cli.tmux._read_log_file(log_path, lines, grep, since)
                print_result({"task": task, "lines": output})
            else:
                print_result({"task": task, "lines": []})
        else:
            tasks_logs: dict[str, list[str]] = {}
            for name in cli.config.tasks:
                log_path = cli.tmux.getLogPath(name)
                if log_path:
                    tasks_logs[name] = cli.tmux._read_log_file(log_path, lines, grep, since)
                else:
                    tasks_logs[name] = []
            print_result({"tasks": tasks_logs})
        return
    cli.tmux.show_logs(task, follow, lines, grep=grep, context=context, since=since)


@app.command(name="logs-clean")
def logs_clean(
    task: str | None = typer.Argument(None, help="Task name (omit for all)"),
):
    """Delete persistent log files.

    Removes log files from ~/.taskmux/logs/. Specify a task name to clean only
    that task's logs, or omit to clean all logs for the current session.
    """
    from pathlib import Path

    cli = TaskmuxCLI()
    log_dir = Path.home() / ".taskmux" / "logs" / cli.config.name

    if not log_dir.exists():
        if is_json_mode():
            print_result({"ok": True, "deleted": 0})
        else:
            console.print("No log files found")
        return

    if task:
        count = 0
        for f in log_dir.glob(f"{task}.log*"):
            f.unlink()
            count += 1
        if is_json_mode():
            print_result({"ok": True, "task": task, "deleted": count})
        else:
            console.print(f"Deleted {count} log file(s) for '{task}'")
    else:
        import shutil

        shutil.rmtree(log_dir)
        if is_json_mode():
            print_result({"ok": True, "session": cli.config.name, "action": "logs_cleaned"})
        else:
            console.print(f"Deleted all logs for session '{cli.config.name}'")


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
    # inspect always outputs JSON regardless of --json flag
    if is_json_mode():
        print_result(data)
    else:
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
    if is_json_mode():
        print_result({"ok": True, "task": task, "command": command, "action": "added"})
    else:
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
    if is_json_mode():
        print_result({"ok": removed, "task": task, "action": "removed"})
    elif removed:
        console.print(f"Removed task '{task}'")
    else:
        console.print(f"Task '{task}' not found in config", style="red")


def _status():
    """Show session and task status.

    Lists all tasks with health indicators, running state, ports, restart policy
    (if non-default), working directory, and dependencies. Aliases: list, ls.
    """
    cli = TaskmuxCLI()
    data = cli.tmux.list_tasks()
    if is_json_mode():
        print_result(data)
        return

    # Human-readable output
    session = data["session"]
    running = data["running"]
    console.print(f"Session '{session}': {'Running' if running else 'Stopped'}")
    if running:
        console.print(f"Active tasks: {data['active_tasks']}")
    console.print("-" * 70)

    if not data["tasks"]:
        console.print("No tasks configured")
        return

    from .models import RestartPolicy

    for t in data["tasks"]:
        health_icon = "G" if t["healthy"] else "R" if t["running"] else "o"
        status_text = "Healthy" if t["healthy"] else "Running" if t["running"] else "Stopped"
        auto = "" if t["auto_start"] else " [manual]"
        port = f" :{t['port']}" if t.get("port") else ""
        extras = ""
        if t.get("restart_policy") and t["restart_policy"] != str(RestartPolicy.ON_FAILURE):
            extras += f" restart={t['restart_policy']}"
        if t.get("cwd"):
            extras += f" cwd={t['cwd']}"
        if t.get("depends_on"):
            extras += f" deps=[{','.join(t['depends_on'])}]"
        line = f"{health_icon} {status_text:8} {t['name']:15}{port:7} {t['command']}"
        console.print(f"{line}{auto}{extras}")


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
        if is_json_mode():
            print_result({"healthy_count": 0, "total_count": 0, "tasks": []})
        else:
            console.print("No session running", style="yellow")
        return

    healthy_count = 0
    total_count = len(cli.config.tasks)
    tasks_health: list[dict] = []

    for task_name in cli.config.tasks:
        is_healthy = cli.tmux.check_task_health(task_name)
        tasks_health.append({"name": task_name, "healthy": is_healthy})
        if is_healthy:
            healthy_count += 1

    if is_json_mode():
        print_result(
            {
                "healthy_count": healthy_count,
                "total_count": total_count,
                "tasks": tasks_health,
            }
        )
        return

    table = Table(title="Health Check Results")
    table.add_column("Status", style="cyan")
    table.add_column("Task", style="magenta")
    table.add_column("Health", style="green")

    for t in tasks_health:
        icon = "G" if t["healthy"] else "R"
        text = "Healthy" if t["healthy"] else "Unhealthy"
        table.add_row(icon, t["name"], text)

    console.print(table)
    console.print(f"Health: {healthy_count}/{total_count} tasks healthy")


@app.command()
def events(
    task: str | None = typer.Option(None, "--task", help="Filter by task name"),
    since: str | None = typer.Option(None, "--since", help="Time filter (e.g. 10m, 1h, 2d)"),
    limit: int = typer.Option(50, "-n", "--limit", help="Max events to show"),
):
    """Show recent lifecycle events.

    Displays task start/stop/restart/kill events, health check failures,
    auto-restarts, and config reloads. Stored at ~/.taskmux/events.jsonl.
    """
    from .events import queryEvents
    from .tmux_manager import _parseSince

    since_dt = _parseSince(since) if since else None
    results = queryEvents(task=task, since=since_dt, limit=limit)

    if is_json_mode():
        print_result({"events": results, "count": len(results)})
        return

    if not results:
        console.print("No events found")
        return

    for ev in results:
        ts = ev["ts"][:19]
        task_str = f" [{ev['task']}]" if "task" in ev else ""
        extra_parts = []
        for k, v in ev.items():
            if k not in ("ts", "event", "task", "session"):
                extra_parts.append(f"{k}={v}")
        extra = f" ({', '.join(extra_parts)})" if extra_parts else ""
        console.print(f"{ts}{task_str} {ev['event']}{extra}")


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
    """Main entry point for the CLI — global exception boundary."""
    try:
        app()
    except TaskmuxError as e:
        print_error(e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        from .errors import ErrorCode

        err = TaskmuxError(ErrorCode.INTERNAL, detail=str(e))
        print_error(err)
        sys.exit(1)


if __name__ == "__main__":
    main()
