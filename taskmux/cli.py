"""Typer-based CLI interface for Taskmux."""

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import List, Optional  # noqa: UP035

import typer
from rich.console import Console
from rich.table import Table

from .config import ProjectIdentity, addTask, loadProjectIdentity, removeTask
from .daemon import (
    SimpleConfigWatcher,
    TaskmuxDaemon,
    get_daemon_pid,
)
from .errors import TaskmuxError
from .init import initProject
from .models import TaskmuxConfig
from .output import is_json_mode, print_error, print_result, set_json_mode
from .paths import ensureTaskmuxDir, globalDaemonLogPath
from .paths import migrate as migrateLayout
from .registry import (
    listRegistered,
    registerProject,
    unregisterProject,
)
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
        if code == "E301":
            console.print(
                "  Hint: use `taskmux restart` to relaunch tasks against the "
                "current config, or `taskmux stop` first.",
                style="yellow",
            )
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
    """Main CLI application class. Optionally bound to a specific config path."""

    def __init__(self, config_path: Path | None = None):
        self.config_path: Path = (config_path or Path("taskmux.toml")).expanduser().resolve()
        self.identity: ProjectIdentity = loadProjectIdentity(self.config_path)
        self.config: TaskmuxConfig = self.identity.config
        self.tmux = TmuxManager(
            self.config,
            config_dir=self.config_path.parent,
            project_id=self.identity.project_id,
            worktree_id=self.identity.worktree_id,
        )

    @property
    def project_id(self) -> str:
        return self.identity.project_id

    def reload_config(self) -> None:
        """Reload config from self.config_path and rebind tmux manager."""
        self.identity = loadProjectIdentity(self.config_path)
        self.config = self.identity.config
        self.tmux.config = self.config
        # project_id can change if user toggles worktree.enabled or renames branches
        self.tmux.project_id = self.identity.project_id
        self.tmux.worktree_id = self.identity.worktree_id

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


def _warn_unprivileged_daemon() -> None:
    """Print a terminal warning before spawning/foregrounding the daemon when
    it'll fail to bind :443 / write /etc/resolver. The daemon logs its own
    warning too, but that goes to ~/.taskmux/daemon.log — easy to miss when
    you ran `taskmux daemon` from a shell."""
    import os as _os

    from .global_config import loadGlobalConfig

    cfg = loadGlobalConfig()
    if _os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
        return
    if not cfg.proxy_enabled:
        return
    if hasattr(_os, "geteuid") and _os.geteuid() == 0:
        return
    needs: list[str] = []
    if cfg.proxy_https_port < 1024:
        needs.append(f"bind :{cfg.proxy_https_port}")
    if cfg.host_resolver in ("etc_hosts", "dns_server"):
        target = (
            "/etc/hosts"
            if cfg.host_resolver == "etc_hosts"
            else f"/etc/resolver/{cfg.dns_managed_tld}"
        )
        needs.append(f"write {target}")
    if not needs:
        return
    if not is_json_mode():
        console.print(
            f"[yellow]Warning: starting daemon without root — these will fail: "
            f"{', '.join(needs)}. Use `sudo taskmux daemon` for proxy + DNS to work.[/yellow]"
        )


def _spawn_detached_daemon(port: int | None = None) -> int | None:
    """Fork the global taskmux daemon as a detached background process.

    When `port` is given, it's forwarded as `--port <port>` so the spawned
    process binds the requested port; otherwise the daemon resolves it from
    `~/.taskmux/config.toml`.
    """
    import subprocess

    existing = get_daemon_pid()
    if existing is not None:
        return existing
    ensureTaskmuxDir()
    log_fh = open(globalDaemonLogPath(), "ab")  # noqa: SIM115
    cmd = [sys.executable, "-m", "taskmux", "daemon"]
    if port is not None:
        cmd += ["--port", str(port)]
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    for _ in range(20):
        if get_daemon_pid() is not None:
            break
        import time as _t

        _t.sleep(0.1)
    return proc.pid


def _notify_daemon_resync(session: str, timeout: float = 1.0) -> None:
    """Best-effort WS notify so the running daemon resyncs proxy routes.

    Fired after CLI lifecycle ops (start/stop/restart/kill) since the CLI's
    local TmuxManager has no callback wired into the daemon's proxy. Silently
    no-op if no daemon, no daemon at the configured port, or any failure.
    """
    if get_daemon_pid() is None:
        return
    try:
        import websockets

        from .global_config import loadGlobalConfig

        port = loadGlobalConfig().api_port
    except Exception:  # noqa: BLE001
        return

    async def _go() -> None:
        try:
            async with websockets.connect(
                f"ws://localhost:{port}", open_timeout=timeout, close_timeout=timeout
            ) as ws:
                await ws.send(json.dumps({"command": "resync", "params": {"session": session}}))
                await asyncio.wait_for(ws.recv(), timeout=timeout)
        except Exception:  # noqa: BLE001
            return

    with contextlib.suppress(Exception):
        asyncio.run(_go())


def _autoRegisterCwd() -> None:
    """Best-effort auto-register of the cwd's project. Swallows collisions."""
    cfg_path = Path("taskmux.toml")
    if not cfg_path.exists():
        return
    try:
        cli_local = TaskmuxCLI(config_path=cfg_path)
    except Exception:  # noqa: BLE001
        return
    try:
        registerProject(cli_local.project_id, cli_local.config_path)
    except TaskmuxError as e:
        if not is_json_mode():
            console.print(f"[yellow]Auto-register skipped:[/yellow] {e.message}")


@app.command()
def start(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
    monitor: bool = typer.Option(  # noqa: B008
        False, "-m", "--monitor", help="Stay running, auto-restart per restart_policy"
    ),
    daemon: bool = typer.Option(  # noqa: B008
        False, "-d", "--daemon", help="Spawn detached daemon for auto-restart + WS API"
    ),
):
    """Start tasks (all auto_start tasks if none specified).

    Starts tasks in dependency order, waiting for each dependency's health check
    to pass before starting dependents. With --monitor, stays in the foreground
    and auto-restarts tasks according to their restart_policy (no/on-failure/always),
    respecting health_retries, max_restarts, and exponential backoff. With --daemon,
    spawns a detached background daemon that does the same plus a WebSocket API.
    """
    import time

    cli = TaskmuxCLI()
    if tasks:
        results = [cli.tmux.start_task(t) for t in tasks]
        _handle_results(results)
    else:
        result = cli.tmux.start_all()
        _handle_result(result)

    # Auto-register cwd project with the registry (idempotent, swallows collisions).
    try:
        registerProject(cli.project_id, cli.config_path)
    except TaskmuxError as e:
        if not is_json_mode():
            console.print(f"[yellow]Auto-register skipped:[/yellow] {e.message}")

    _notify_daemon_resync(cli.project_id)

    if daemon or cli.config.auto_daemon:
        pid = _spawn_detached_daemon()
        if not is_json_mode():
            if pid:
                console.print(f"Daemon started (pid {pid})")
            else:
                console.print("Daemon failed to start", style="red")

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
    _notify_daemon_resync(cli.project_id)


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
    _notify_daemon_resync(cli.project_id)


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
    _notify_daemon_resync(cli.project_id)


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

    Removes log files from ~/.taskmux/projects/{session}/logs/. Specify a task
    name to clean only that task's logs, or omit to clean all logs for the
    current session.
    """
    from .paths import projectLogsDir

    cli = TaskmuxCLI()
    log_dir = projectLogsDir(cli.config.name, cli.identity.worktree_id)

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
            print_result({"ok": True, "session": cli.project_id, "action": "logs_cleaned"})
        else:
            console.print(f"Deleted all logs for session '{cli.project_id}'")


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
    host: str | None = typer.Option(  # noqa: B008
        None, "--host", help="Subdomain to expose via the proxy: {host}.{project}.localhost"
    ),
    health_check: str | None = typer.Option(None, "--health-check", help="Health check command"),
    depends_on: Optional[List[str]] = typer.Option(  # noqa: UP006, UP045, B008
        None, "--depends-on", help="Dependency task names"
    ),
):
    """Add a new task to taskmux.toml."""
    addTask(
        None,
        task,
        command,
        cwd=cwd,
        host=host,
        health_check=health_check,
        depends_on=depends_on,
    )
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
    daemon_pid = get_daemon_pid()
    data["daemon_pid"] = daemon_pid
    if is_json_mode():
        print_result(data)
        return

    # Human-readable output
    session = data["session"]
    running = data["running"]
    console.print(f"Session '{session}': {'Running' if running else 'Stopped'}")
    if running:
        console.print(f"Active tasks: {data['active_tasks']}")

    from .models import RestartPolicy

    if daemon_pid:
        console.print(f"Auto-restart: active (pid {daemon_pid})", style="green")
    else:
        any_restart = any(
            t.get("restart_policy") and t["restart_policy"] != str(RestartPolicy.NO)
            for t in data["tasks"]
        )
        if any_restart:
            console.print(
                "Auto-restart: inactive — run 'taskmux daemon' or 'taskmux start -d' to enable",
                style="yellow",
            )

    proxy = data.get("proxy")
    if proxy and not proxy.get("bound"):
        console.print(f"Proxy: {proxy['reason']}", style="yellow")

    console.print("-" * 70)

    if not data["tasks"]:
        console.print("No tasks configured")
        return

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Status", style="cyan", no_wrap=True)
    table.add_column("Task", style="magenta", no_wrap=True)
    table.add_column("URL", no_wrap=True)
    table.add_column("Command", overflow="ellipsis", no_wrap=True)
    table.add_column("Notes", style="dim", no_wrap=True)

    fail_rows: list[tuple[str, str, str]] = []
    for t in data["tasks"]:
        health_icon = "G" if t["healthy"] else "R" if t["running"] else "o"
        icon_style = "green" if t["healthy"] else "red" if t["running"] else "dim"
        status_text = "Healthy" if t["healthy"] else "Running" if t["running"] else "Stopped"
        notes: list[str] = []
        if not t["auto_start"]:
            notes.append("manual")
        if t.get("restart_policy") and t["restart_policy"] != str(RestartPolicy.ON_FAILURE):
            notes.append(f"restart={t['restart_policy']}")
        if t.get("cwd"):
            notes.append(f"cwd={t['cwd']}")
        if t.get("depends_on"):
            notes.append(f"deps=[{','.join(t['depends_on'])}]")
        table.add_row(
            f"[{icon_style}]{health_icon}[/{icon_style}]",
            status_text,
            t["name"],
            t.get("url") or "",
            t["command"],
            " ".join(notes),
        )
        last = t.get("last_health")
        if last and not last.get("ok") and last.get("reason"):
            fail_rows.append((t["name"], last.get("method", ""), last.get("reason", "")))

    console.print(table)
    for name, method, reason in fail_rows:
        console.print(f"    {name} fail: {method} — {reason}", style="red")

    aliases = data.get("aliases") or []
    if aliases:
        console.print()
        console.print("Aliases (external routes):", style="bold")
        atable = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        atable.add_column("Name", style="magenta")
        atable.add_column("URL")
        atable.add_column("Target", style="dim")
        for a in aliases:
            atable.add_row(a["name"], a["url"], f"127.0.0.1:{a['port']}")
        console.print(atable)


app.command(name="status")(_status)
app.command(name="list", hidden=True)(_status)
app.command(name="ls", hidden=True)(_status)


@app.command()
def health(
    verbose: bool = typer.Option(  # noqa: B008
        False, "-v", "--verbose", help="Show probe method and failure reasons"
    ),
):
    """Check health of all tasks.

    Runs each task's probe (health_url → health_check → tcp(port) → pane-alive).
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
        result = cli.tmux.check_health(task_name)
        tasks_health.append(
            {
                "name": task_name,
                "healthy": result.ok,
                "method": result.method,
                "reason": result.reason,
            }
        )
        if result.ok:
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
    if verbose:
        table.add_column("Method")
        table.add_column("Reason")

    for t in tasks_health:
        icon = "G" if t["healthy"] else "R"
        text = "Healthy" if t["healthy"] else "Unhealthy"
        if verbose:
            table.add_row(icon, t["name"], text, t["method"], t["reason"] or "")
        else:
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
def url(
    task: str = typer.Argument(..., help="Task name"),
):
    """Print the proxy URL for a task: https://{host}.{project}.localhost"""
    from .url import taskUrl

    cli = TaskmuxCLI()
    cfg = cli.config.tasks.get(task)
    if cfg is None:
        if is_json_mode():
            print_result({"ok": False, "error": "task_not_found", "task": task})
        else:
            console.print(f"Task '{task}' not found in config", style="red")
        sys.exit(1)
    if cfg.host is None:
        if is_json_mode():
            print_result({"ok": False, "error": "no_host", "task": task})
        else:
            console.print(f"Task '{task}' has no host set (not exposed via proxy)", style="yellow")
        sys.exit(1)
    u = taskUrl(cli.project_id, cfg.host)
    if is_json_mode():
        print_result({"ok": True, "task": task, "url": u})
    else:
        console.print(u)


@app.command()
def watch():
    """Watch taskmux.toml for changes and reload on edit.

    Stays in the foreground. When the config file changes, reloads it and
    restarts affected tasks.
    """
    cli = TaskmuxCLI()
    watcher = SimpleConfigWatcher(cli)
    watcher.watch_config()


daemon_app = typer.Typer(
    name="daemon",
    help=(
        "Daemon lifecycle: start, stop, status, restart.\n\n"
        "Bare 'taskmux daemon' runs a foreground daemon (WS API + health monitor + "
        "config watcher). Use 'daemon start' to spawn detached."
    ),
    no_args_is_help=False,
    invoke_without_command=True,
)


@daemon_app.callback()
def daemon(
    ctx: typer.Context,
    port: int | None = typer.Option(  # noqa: B008
        None, "--port", help="WebSocket API port (overrides ~/.taskmux/config.toml)"
    ),
):
    """Run a foreground daemon when no subcommand is given.

    Health-check cadence and default API port come from ~/.taskmux/config.toml
    (see `taskmux config show`). `--port` overrides the config value.
    """
    if ctx.invoked_subcommand is not None:
        return
    _warn_unprivileged_daemon()
    d = TaskmuxDaemon(api_port=port)
    asyncio.run(d.start())


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
    """Poll until pid is no longer running or timeout elapses. Returns True if exited."""
    import os
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        _t.sleep(0.1)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


@daemon_app.command("start")
def daemon_start(
    port: int | None = typer.Option(  # noqa: B008
        None, "--port", help="WebSocket API port (overrides ~/.taskmux/config.toml)"
    ),
):
    """Spawn the global detached daemon (idempotent).

    Auto-registers cwd's project if a `taskmux.toml` is present. Logs go to
    `~/.taskmux/daemon.log`.
    """
    existing = get_daemon_pid()
    if existing is not None:
        _autoRegisterCwd()
        if is_json_mode():
            print_result({"ok": True, "pid": existing, "action": "already_running"})
        else:
            console.print(f"Daemon already running (pid {existing})")
        return
    _autoRegisterCwd()
    _warn_unprivileged_daemon()
    pid = _spawn_detached_daemon(port=port)
    if pid is None:
        if is_json_mode():
            print_result({"ok": False, "error": "failed to start daemon"})
        else:
            console.print("Daemon failed to start", style="red")
        return
    if is_json_mode():
        print_result({"ok": True, "pid": pid, "action": "started"})
    else:
        console.print(f"Daemon started (pid {pid})")


@daemon_app.command("pid")
def daemon_pid():
    """Print the daemon PID (just the integer; empty if not running).

    Useful for scripting: `kill $(taskmux daemon pid)` or
    `lsof -p $(taskmux daemon pid)`. Exits 1 if no daemon is running.
    """
    pid = get_daemon_pid()
    if pid is None:
        if is_json_mode():
            print_result({"ok": False, "pid": None})
        sys.exit(1)
    if is_json_mode():
        print_result({"ok": True, "pid": pid})
    else:
        print(pid)


@daemon_app.command("stop")
def daemon_stop():
    """SIGTERM the global daemon."""
    import os
    import signal as _sig

    pid = get_daemon_pid()
    if pid is None:
        if is_json_mode():
            print_result({"ok": False, "error": "daemon not running"})
        else:
            console.print("No daemon running")
        return
    try:
        os.kill(pid, _sig.SIGTERM)
    except OSError as e:
        if is_json_mode():
            print_result({"ok": False, "error": str(e)})
        else:
            console.print(f"Failed to signal daemon: {e}", style="red")
        return
    if is_json_mode():
        print_result({"ok": True, "pid": pid, "action": "stopped"})
    else:
        console.print(f"Sent SIGTERM to daemon (pid {pid})")


@daemon_app.command("status")
def daemon_status():
    """Show daemon status + count of registered projects."""
    pid = get_daemon_pid()
    entries = listRegistered()
    if is_json_mode():
        print_result({"running": pid is not None, "pid": pid, "registered_projects": len(entries)})
        return
    if pid is not None:
        console.print(f"Daemon running (pid {pid}) — {len(entries)} project(s) registered")
    else:
        console.print(f"No daemon running — {len(entries)} project(s) registered")


@daemon_app.command("restart")
def daemon_restart(
    port: int | None = typer.Option(  # noqa: B008
        None, "--port", help="WebSocket API port (overrides ~/.taskmux/config.toml)"
    ),
):
    """Stop the global daemon (if any) and spawn a fresh one."""
    import os
    import signal as _sig

    pid = get_daemon_pid()
    if pid is not None:
        try:
            os.kill(pid, _sig.SIGTERM)
        except OSError as e:
            if is_json_mode():
                print_result({"ok": False, "error": f"failed to stop: {e}"})
            else:
                console.print(f"Failed to stop daemon: {e}", style="red")
            return
        if not _wait_for_pid_exit(pid, timeout=5.0):
            if is_json_mode():
                print_result({"ok": False, "error": f"daemon pid {pid} did not exit"})
            else:
                console.print(f"Daemon pid {pid} did not exit within 5s", style="red")
            return
    new_pid = _spawn_detached_daemon(port=port)
    if new_pid is None:
        if is_json_mode():
            print_result({"ok": False, "error": "failed to start daemon"})
        else:
            console.print("Daemon failed to start", style="red")
        return
    if is_json_mode():
        print_result({"ok": True, "pid": new_pid, "action": "restarted", "old_pid": pid})
    else:
        console.print(f"Daemon restarted (pid {new_pid})")


@daemon_app.command("list")
def daemon_list(
    port: int | None = typer.Option(  # noqa: B008
        None, "--port", help="Daemon WS port (default: ~/.taskmux/config.toml)"
    ),
):
    """List all registered projects + live daemon view (if running)."""
    from .global_config import loadGlobalConfig

    pid = get_daemon_pid()
    registered = listRegistered()
    live: dict[str, dict] = {}

    if pid is not None and registered:
        live_port = port if port is not None else loadGlobalConfig().api_port
        live = _query_live_projects(port=live_port)

    if is_json_mode():
        out = []
        for entry in registered:
            row = {
                "session": entry["session"],
                "config_path": entry["config_path"],
                "registered_at": entry["registered_at"],
            }
            row.update(live.get(entry["session"], {}))
            out.append(row)
        print_result({"projects": out, "daemon_pid": pid, "count": len(out)})
        return

    if not registered:
        console.print("No projects registered. Run `taskmux start` in a project to auto-register.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Session")
    table.add_column("Project")
    table.add_column("Worktree")
    table.add_column("State", justify="left")
    table.add_column("Tmux", justify="left")
    table.add_column("Tasks", justify="right")
    table.add_column("Config")
    for entry in registered:
        info = live.get(entry["session"], {})
        state = info.get("state", "[dim]unmanaged[/dim]" if pid is None else "ok")
        tmux_state = "[green]up[/green]" if info.get("session_exists") else "[dim]down[/dim]"
        task_count = info.get("task_count", "?")
        project = info.get("project", entry["session"])
        worktree = info.get("worktree") or "[dim]—[/dim]"
        table.add_row(
            entry["session"],
            str(project),
            str(worktree),
            str(state),
            tmux_state,
            str(task_count),
            entry["config_path"],
        )
    console.print(table)
    if pid is None:
        console.print(
            "[yellow]Daemon not running. Start with `taskmux daemon start` for live state.[/yellow]"
        )


@daemon_app.command("register")
def daemon_register(
    config: str | None = typer.Option(  # noqa: B008
        None, "--config", "-c", help="Path to taskmux.toml (default: cwd)"
    ),
    force: bool = typer.Option(  # noqa: B008
        False,
        "--force",
        "-f",
        help="Overwrite an existing registration with a different config path.",
    ),
):
    """Add a project to the registry. Daemon (if running) picks it up live.

    A registry slot bound to a path that no longer exists on disk is
    auto-healed (no flag needed). Use --force when both paths still exist
    and you want the new one to win.
    """
    cfg_path = Path(config).expanduser() if config else Path("taskmux.toml")
    if not cfg_path.exists():
        if is_json_mode():
            print_result({"ok": False, "error": f"config not found: {cfg_path}"})
        else:
            console.print(f"Config not found: {cfg_path}", style="red")
        sys.exit(1)
    cli_local = TaskmuxCLI(config_path=cfg_path)
    entry = registerProject(cli_local.project_id, cli_local.config_path, force=force)
    if is_json_mode():
        print_result({"ok": True, "action": "registered", "entry": dict(entry)})
    else:
        console.print(f"Registered '{entry['session']}' → {entry['config_path']}", style="green")


@daemon_app.command("unregister")
def daemon_unregister(
    session: str = typer.Argument(..., help="Session name to remove"),
):
    """Remove a project from the registry. Daemon picks it up live."""
    removed = unregisterProject(session)
    if not removed:
        if is_json_mode():
            print_result({"ok": False, "error": "session_not_registered", "session": session})
        else:
            console.print(f"Session '{session}' not in registry", style="yellow")
        sys.exit(1)
    if is_json_mode():
        print_result({"ok": True, "action": "unregistered", "session": session})
    else:
        console.print(f"Unregistered '{session}'", style="green")


def _query_live_projects(port: int = 8765, timeout: float = 1.0) -> dict[str, dict]:
    """Best-effort WS query of the live daemon. Returns {} on any failure."""
    try:
        import websockets
    except ImportError:
        return {}

    async def _go() -> dict[str, dict]:
        try:
            async with websockets.connect(
                f"ws://localhost:{port}", open_timeout=timeout, close_timeout=timeout
            ) as ws:
                await ws.send(json.dumps({"command": "list_projects"}))
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                resp = json.loads(raw)
                projects = resp.get("projects", [])
                return {p["session"]: p for p in projects}
        except Exception:  # noqa: BLE001
            return {}

    try:
        return asyncio.run(_go())
    except Exception:  # noqa: BLE001
        return {}


app.add_typer(daemon_app)


# ---------------------------------------------------------------------------
# Global config sub-app
# ---------------------------------------------------------------------------

config_app = typer.Typer(
    name="config",
    help="Inspect and edit ~/.taskmux/config.toml (host-wide settings).",
    no_args_is_help=True,
)


@config_app.command("show")
def config_show():
    """Print the resolved global config (defaults + overrides)."""
    from .global_config import loadGlobalConfig
    from .paths import globalConfigPath

    cfg = loadGlobalConfig()
    path = globalConfigPath()
    data = cfg.model_dump()
    if is_json_mode():
        print_result({"path": str(path), "exists": path.exists(), "config": data})
        return
    suffix = "" if path.exists() else " [dim](does not exist — using defaults)[/dim]"
    console.print(f"Config: {path}{suffix}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Key")
    table.add_column("Value", justify="right")
    for k, v in data.items():
        table.add_row(k, str(v))
    console.print(table)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key, e.g. health_check_interval"),
    value: str = typer.Argument(..., help="New value (parsed as int/bool/string)"),
):
    """Set a global config key. Coerces value to int/bool when possible."""
    from .errors import ErrorCode
    from .global_config import GlobalConfig, updateGlobalConfig

    if key not in GlobalConfig.model_fields:
        valid = ", ".join(sorted(GlobalConfig.model_fields))
        raise TaskmuxError(
            ErrorCode.CONFIG_VALIDATION,
            detail=f"unknown config key '{key}' (valid: {valid})",
        )

    parsed: object = value
    if value.lower() in {"true", "false"}:
        parsed = value.lower() == "true"
    else:
        with contextlib.suppress(ValueError):
            parsed = int(value)
    new = updateGlobalConfig({key: parsed})
    if is_json_mode():
        print_result({"ok": True, "key": key, "value": getattr(new, key, parsed)})
    else:
        console.print(f"Set {key} = {getattr(new, key, parsed)}", style="green")


@config_app.command("path")
def config_path():
    """Print the path to the global config file."""
    from .paths import globalConfigPath

    p = globalConfigPath()
    if is_json_mode():
        print_result({"path": str(p), "exists": p.exists()})
    else:
        console.print(str(p))


app.add_typer(config_app)


# ---------------------------------------------------------------------------
# CA sub-app (mkcert wrapper)
# ---------------------------------------------------------------------------

ca_app = typer.Typer(
    name="ca",
    help="Local CA management for the proxy (wraps mkcert).",
    no_args_is_help=True,
)


@ca_app.command("install")
def ca_install():
    """Run `mkcert -install` to trust the local CA in your system store."""
    from .ca import MkcertMissing, ensureCAInstalled

    try:
        ensureCAInstalled()
    except MkcertMissing as e:
        if is_json_mode():
            print_result({"ok": False, "error": e.message})
        else:
            console.print(e.message, style="red")
        sys.exit(1)
    if is_json_mode():
        print_result({"ok": True, "action": "installed"})
    else:
        console.print("Local CA installed (mkcert -install).")


@ca_app.command("mint")
def ca_mint():
    """Mint a wildcard cert for the current project: *.{project}.localhost."""
    from .ca import MkcertMissing, mintCert

    cli = TaskmuxCLI()
    try:
        cert, key = mintCert(cli.project_id)
    except MkcertMissing as e:
        if is_json_mode():
            print_result({"ok": False, "error": e.message})
        else:
            console.print(e.message, style="red")
        sys.exit(1)
    if is_json_mode():
        print_result({"ok": True, "project": cli.project_id, "cert": str(cert), "key": str(key)})
    else:
        console.print(f"Cert: {cert}")
        console.print(f"Key:  {key}")


app.add_typer(ca_app)


# ---------------------------------------------------------------------------
# DNS sub-app — manage in-process DNS server delegation
# ---------------------------------------------------------------------------

dns_app = typer.Typer(
    name="dns",
    help="Manage the in-process DNS server delegation (host_resolver = 'dns_server').",
    no_args_is_help=True,
)


@dns_app.command("install")
def dns_install_cmd():
    """Install OS-level DNS delegation for the configured TLD.

    Writes /etc/resolver/<tld> on macOS, systemd-resolved drop-in on Linux,
    or NRPT rule on Windows. Requires root/Admin. Idempotent.
    """
    from . import dns_install
    from .global_config import loadGlobalConfig

    cfg = loadGlobalConfig()
    try:
        dns_install.installDelegation(cfg.dns_managed_tld, cfg.dns_server_port)
        dns_install.flushDnsCache()
    except (PermissionError, OSError, RuntimeError) as e:
        if is_json_mode():
            print_result({"ok": False, "error": str(e)})
        else:
            console.print(f"DNS install failed: {e}", style="red")
        sys.exit(1)
    if is_json_mode():
        print_result({"ok": True, "tld": cfg.dns_managed_tld, "port": cfg.dns_server_port})
    else:
        console.print(
            f"DNS delegation installed: .{cfg.dns_managed_tld} -> 127.0.0.1:{cfg.dns_server_port}"
        )


@dns_app.command("uninstall")
def dns_uninstall_cmd():
    """Remove OS-level DNS delegation."""
    from . import dns_install
    from .global_config import loadGlobalConfig

    cfg = loadGlobalConfig()
    try:
        dns_install.uninstallDelegation(cfg.dns_managed_tld)
        dns_install.flushDnsCache()
    except (PermissionError, OSError) as e:
        if is_json_mode():
            print_result({"ok": False, "error": str(e)})
        else:
            console.print(f"DNS uninstall failed: {e}", style="red")
        sys.exit(1)
    if is_json_mode():
        print_result({"ok": True, "tld": cfg.dns_managed_tld})
    else:
        console.print(f"DNS delegation removed for .{cfg.dns_managed_tld}")


@dns_app.command("flush")
def dns_flush_cmd():
    """Flush the OS DNS cache."""
    from . import dns_install

    dns_install.flushDnsCache()
    if is_json_mode():
        print_result({"ok": True})
    else:
        console.print("DNS cache flushed")


@dns_app.command("query")
def dns_query_cmd(
    name: str = typer.Argument(..., help="Hostname to look up"),
    qtype: str = typer.Option("A", "--type", help="Record type (A, AAAA)"),
):
    """Query the running taskmux DNS server directly (debug helper)."""
    import socket as _sock

    from dnslib import QTYPE, RCODE, DNSRecord

    from .global_config import loadGlobalConfig

    cfg = loadGlobalConfig()
    qt = QTYPE[qtype.upper()]
    q = DNSRecord.question(name, qtype=qtype.upper())
    with _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM) as s:
        s.settimeout(2.0)
        try:
            s.sendto(q.pack(), ("127.0.0.1", cfg.dns_server_port))
            data, _ = s.recvfrom(4096)
        except OSError as e:
            if is_json_mode():
                print_result({"ok": False, "error": str(e)})
            else:
                console.print(
                    f"DNS query failed: {e} — is the daemon running with "
                    f"host_resolver = 'dns_server'?",
                    style="red",
                )
            sys.exit(1)
    rec = DNSRecord.parse(data)
    answers = [str(rr.rdata) for rr in rec.rr if rr.rtype == qt]
    if is_json_mode():
        print_result(
            {
                "ok": True,
                "name": name,
                "type": qtype.upper(),
                "rcode": RCODE[rec.header.rcode],
                "answers": answers,
            }
        )
    else:
        rcode = RCODE[rec.header.rcode]
        if rcode != "NOERROR":
            console.print(f"{name} {qtype.upper()} -> {rcode}", style="yellow")
        elif not answers:
            console.print(f"{name} {qtype.upper()} -> (empty)")
        else:
            for a in answers:
                console.print(a)


app.add_typer(dns_app)


# ---------------------------------------------------------------------------
# Worktree sub-app — git worktree-aware project rows
# ---------------------------------------------------------------------------

worktree_app = typer.Typer(
    name="worktree",
    help="Inspect git-worktree-scoped sessions for the current repo.",
    no_args_is_help=True,
)


def _worktreeRowsForRepo(primary_path: Path | None) -> list[dict]:
    """Cross-reference registry entries against a repo's primary worktree path.

    A registry entry belongs to this repo iff loading its config + detecting
    its worktree yields the same `primary_worktree_path` as the cwd's identity.
    When the caller is inside a repo, entries with no detected primary path
    (e.g. a registered project living outside any git checkout) are excluded
    — they can't be siblings. Falls back to listing every registered entry
    only when the caller itself is not in a repo.
    """
    rows: list[dict] = []
    for entry in listRegistered():
        cfg_path = Path(entry["config_path"])
        if not cfg_path.exists():
            continue
        try:
            ident = loadProjectIdentity(cfg_path)
        except Exception:  # noqa: BLE001
            continue
        if primary_path is not None and ident.primary_worktree_path != primary_path:
            continue
        rows.append(
            {
                "session": entry["session"],
                "project": ident.project,
                "worktree": ident.worktree_id,
                "branch": ident.branch,
                "path": str(ident.worktree_path) if ident.worktree_path else None,
                "config_path": entry["config_path"],
            }
        )
    return rows


@worktree_app.command("status")
def worktree_status():
    """Show the current cwd's project/worktree identity."""
    cli = TaskmuxCLI()
    ident = cli.identity
    payload = {
        "project": ident.project,
        "project_id": ident.project_id,
        "worktree": ident.worktree_id,
        "branch": ident.branch,
        "worktree_path": str(ident.worktree_path) if ident.worktree_path else None,
        "primary_worktree_path": (
            str(ident.primary_worktree_path) if ident.primary_worktree_path else None
        ),
        "is_linked": ident.worktree_id is not None,
        "config_path": str(ident.config_path),
    }
    if is_json_mode():
        print_result(payload)
        return
    console.print(f"Project:    {payload['project']}")
    console.print(f"Project ID: {payload['project_id']}")
    console.print(f"Worktree:   {payload['worktree'] or '[dim](primary)[/dim]'}")
    console.print(f"Branch:     {payload['branch'] or '[dim](detached)[/dim]'}")
    console.print(f"Path:       {payload['worktree_path'] or '[dim](no repo)[/dim]'}")


@worktree_app.command("list")
def worktree_list():
    """List all worktrees of the current repo with their session state."""
    cli = TaskmuxCLI()
    rows = _worktreeRowsForRepo(cli.identity.primary_worktree_path)
    if is_json_mode():
        print_result({"worktrees": rows, "count": len(rows)})
        return
    if not rows:
        console.print("No registered worktrees for this repo.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Session")
    table.add_column("Worktree")
    table.add_column("Branch")
    table.add_column("Path")
    for row in rows:
        table.add_row(
            row["session"],
            row["worktree"] or "[dim](primary)[/dim]",
            row["branch"] or "[dim]—[/dim]",
            row["path"] or "[dim]—[/dim]",
        )
    console.print(table)


@worktree_app.command("urls")
def worktree_urls():
    """Print proxy URLs for all hosted tasks in the current worktree."""
    from .url import taskUrl

    cli = TaskmuxCLI()
    out: list[dict] = []
    for task_name, task_cfg in cli.config.tasks.items():
        if task_cfg.host is None:
            continue
        out.append(
            {
                "task": task_name,
                "host": task_cfg.host,
                "url": taskUrl(cli.project_id, task_cfg.host),
            }
        )
    if is_json_mode():
        print_result({"project_id": cli.project_id, "urls": out})
        return
    if not out:
        console.print("No tasks with `host` set in config.")
        return
    for row in out:
        console.print(f"{row['task']:15} {row['url']}")


app.add_typer(worktree_app)


def main():
    """Main entry point for the CLI — global exception boundary."""
    with contextlib.suppress(Exception):
        migrateLayout()
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
