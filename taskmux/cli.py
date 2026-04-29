"""Typer-based CLI interface for Taskmux.

Thin client over the daemon's WebSocket IPC. Each lifecycle command:
1. resolves the project from cwd's taskmux.toml
2. ensures the daemon is running (auto-spawn detached if needed)
3. auto-registers the project + asks the daemon to sync the registry
4. fires one IPC call with session + task params
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import List, Optional  # noqa: UP035

import typer
from rich.console import Console
from rich.table import Table

from . import ipc_client
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
from .paths import (
    ensureTaskmuxDir,
    globalDaemonLogPath,
    taskLogPath,
)
from .paths import migrate as migrateLayout
from .registry import (
    listRegistered,
    registerProject,
    unregisterProject,
)

TASK_COLORS = ["cyan", "green", "yellow", "magenta", "blue", "red"]

app = typer.Typer(
    name="taskmux",
    help=(
        "Daemon-backed task manager for development environments.\n\n"
        "Reads task definitions from taskmux.toml. The daemon owns all task "
        "processes (PTY-backed, supervised) — CLI commands are thin RPC calls. "
        "Health monitoring, restart policies, dependency ordering, lifecycle "
        "hooks, WebSocket API, and an HTTPS proxy are all daemon-side.\n\n"
        "Quick start: taskmux init → edit taskmux.toml → taskmux start"
    ),
    epilog="Docs: https://github.com/nc9/taskmux",
    rich_markup_mode="rich",
    no_args_is_help=True,
)

console = Console()


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


def _print_result_human(result: dict) -> None:
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
    if is_json_mode():
        print_result(result)
    else:
        _print_result_human(result)


def _handle_results(results: list[dict]) -> None:
    if is_json_mode():
        print_result({"ok": all(r.get("ok") for r in results), "results": results})
    else:
        for r in results:
            _print_result_human(r)


# ---------------------------------------------------------------------------
# CLI helper: just config_path + parsed config. No process supervision.
# ---------------------------------------------------------------------------


class TaskmuxCLI:
    """Thin handle on the cwd's taskmux.toml — resolves project_id + paths."""

    def __init__(self, config_path: Path | None = None):
        self.config_path: Path = (config_path or Path("taskmux.toml")).expanduser().resolve()
        self.identity: ProjectIdentity = loadProjectIdentity(self.config_path)
        self.config: TaskmuxConfig = self.identity.config

    @property
    def project_id(self) -> str:
        return self.identity.project_id

    @property
    def worktree_id(self) -> str | None:
        return self.identity.worktree_id

    def reload_config(self) -> None:
        self.identity = loadProjectIdentity(self.config_path)
        self.config = self.identity.config


# ---------------------------------------------------------------------------
# IPC plumbing — auto-start daemon, auto-register cwd, then call.
# ---------------------------------------------------------------------------


def _ensure_session_known(session: str, config_path: Path) -> None:
    """Register the project + tell the daemon to pick it up synchronously."""
    try:
        registerProject(session, config_path)
    except TaskmuxError as e:
        if not is_json_mode():
            console.print(f"[yellow]Auto-register skipped:[/yellow] {e.message}")
    with contextlib.suppress(Exception):
        ipc_client.call("sync_registry")


def _call_session(command: str, session: str, **params) -> dict:
    """Wrap ipc.call: unwraps the {result: ...} envelope when present."""
    payload = {"session": session, **params}
    resp = ipc_client.call(command, params=payload)
    return resp.get("result", resp)


def _notify_daemon_resync(session: str) -> None:
    """Nudge the daemon to reconcile proxy routes against disk state.

    Best-effort — silently no-ops if no daemon is running. Used by alias
    add/remove and other out-of-band mutators.
    """
    ipc_client.call_no_ensure("resync", params={"session": session})


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
    """Fork the global taskmux daemon as a detached background process."""
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


def _reinjectAgentBlock() -> list[Path]:
    """Re-patch CLAUDE.md / AGENTS.md after a task add/remove. Best-effort.

    Reads the freshly-written taskmux.toml so the rendered task table
    reflects the post-mutation state. Honors `auto_inject_agents` in both
    project and global config.
    """
    from .agent import reinjectIfEnabled

    cfg_path = Path("taskmux.toml")
    if not cfg_path.exists():
        return []
    try:
        cli_local = TaskmuxCLI(config_path=cfg_path)
    except Exception:  # noqa: BLE001
        return []
    return reinjectIfEnabled(cfg_path.resolve().parent, cli_local.config)


def _autoRegisterCwd() -> None:
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


# ---------------------------------------------------------------------------
# Lifecycle — all routed through ipc_client.
# ---------------------------------------------------------------------------


@app.command()
def start(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
    monitor: bool = typer.Option(  # noqa: B008
        False, "-m", "--monitor", help="(deprecated; daemon always supervises)"
    ),
    daemon: bool = typer.Option(  # noqa: B008
        False, "-d", "--daemon", help="(deprecated; daemon always runs)"
    ),
):
    """Start tasks (all auto_start tasks if none specified)."""
    _ = monitor, daemon  # back-compat no-ops
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)
    if tasks:
        results = [_call_session("start", cli.project_id, task=t) for t in tasks]
        _handle_results(results)
    else:
        _handle_result(_call_session("start_all", cli.project_id))


@app.command()
def stop(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
):
    """Stop tasks (all if none specified). Signal escalation: SIGINT → SIGTERM → SIGKILL."""
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)
    if tasks:
        results = [_call_session("stop", cli.project_id, task=t) for t in tasks]
        _handle_results(results)
    else:
        _handle_result(_call_session("stop_all", cli.project_id))


@app.command()
def restart(
    tasks: list[str] = typer.Argument(None, help="Task names (omit for all)"),  # noqa: B008
):
    """Restart tasks (all if none specified). Full stop with escalation, then start."""
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)
    if tasks:
        results = [_call_session("restart", cli.project_id, task=t) for t in tasks]
        _handle_results(results)
    else:
        _handle_result(_call_session("restart_all", cli.project_id))


@app.command()
def kill(
    task: str = typer.Argument(..., help="Task name to kill"),
):
    """Kill a specific task (SIGKILL, no grace)."""
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)
    _handle_result(_call_session("kill", cli.project_id, task=task))


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
    """Show logs for a task, or interleaved logs from all tasks."""
    _ = context  # not yet plumbed through IPC
    cli = TaskmuxCLI()

    if follow:
        # Daemon writes log files; client tails them directly.
        if task is not None:
            _follow_one(cli.identity.project, cli.worktree_id, task, grep)
        else:
            _follow_all(cli.identity.project, cli.worktree_id, list(cli.config.tasks.keys()), grep)
        return

    _ensure_session_known(cli.project_id, cli.config_path)

    if task is not None:
        resp = ipc_client.call(
            "logs",
            params={
                "session": cli.project_id,
                "task": task,
                "lines": lines,
                "grep": grep,
                "since": since,
            },
        )
        out = resp.get("lines", [])
        if is_json_mode():
            print_result({"task": task, "lines": out})
        else:
            for line in out:
                print(line)
        return

    resp = ipc_client.call(
        "logs",
        params={
            "session": cli.project_id,
            "lines": lines,
            "grep": grep,
            "since": since,
        },
    )
    tasks_logs = resp.get("tasks", {})
    if is_json_mode():
        print_result({"tasks": tasks_logs})
    else:
        from rich.markup import escape

        for i, (name, ls) in enumerate(tasks_logs.items()):
            color = TASK_COLORS[i % len(TASK_COLORS)]
            for line in ls:
                console.print(f"[{color}][{escape(name)}][/{color}] {escape(line)}")


def _follow_one(project: str, worktree_id: str | None, task: str, grep: str | None) -> None:
    log_path = taskLogPath(project, task, worktree_id)
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch()
    ipc_client.follow_log_file(log_path, grep=grep)


def _follow_all(
    project: str,
    worktree_id: str | None,
    task_names: list[str],
    grep: str | None,
) -> None:
    triples: list[tuple[str, Path, str]] = []
    for i, name in enumerate(task_names):
        log_path = taskLogPath(project, name, worktree_id)
        if not log_path.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch()
        triples.append((name, log_path, TASK_COLORS[i % len(TASK_COLORS)]))
    ipc_client.follow_log_files(triples, grep=grep)


@app.command(name="logs-clean")
def logs_clean(
    task: str | None = typer.Argument(None, help="Task name (omit for all)"),
):
    """Delete persistent log files (alias for `clean --logs`).

    Removes log files from ~/.taskmux/projects/{session}/logs/. Specify a task
    name to clean only that task's logs, or omit to clean all logs for the
    current session.
    """
    from .cleanup import cleanLogs

    cli = TaskmuxCLI()
    report = cleanLogs(cli.config.name, cli.identity.worktree_id, task=task)
    if is_json_mode():
        print_result({"ok": True, "task": task, "deleted": len(report["deleted"])})
        return
    if not report["deleted"]:
        console.print("No log files found")
    elif task:
        console.print(f"Deleted {len(report['deleted'])} log file(s) for '{task}'")
    else:
        console.print(f"Deleted all logs for session '{cli.project_id}'")


@app.command()
def clean(
    logs: bool = typer.Option(False, "--logs", help="Only delete log files"),  # noqa: B008
    events: bool = typer.Option(  # noqa: B008
        False, "--events", help="Only truncate ~/.taskmux/events.jsonl"
    ),
    certs: bool = typer.Option(  # noqa: B008
        False, "--certs", help="Only remove minted *.localhost certs (mkcert root CA stays)"
    ),
    all_: bool = typer.Option(  # noqa: B008
        False, "--all", help="Wipe ~/.taskmux/ entirely except config.toml"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only, no deletes"),  # noqa: B008
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),  # noqa: B008
    force: bool = typer.Option(  # noqa: B008
        False, "--force", help="Wipe even if session running / daemon up"
    ),
):
    """Wipe taskmux state. Default: current project (logs, state, certs, registry).

    Flags select scope. Multiple are combined. With no scope flag and no
    --all, wipes the current project's per-project state. --all is global
    and refuses while the daemon is running unless --force.
    """
    from .cleanup import (
        cleanAll,
        cleanCerts,
        cleanEvents,
        cleanLogs,
        cleanProjectState,
    )

    scoped = logs or events or certs
    reports: dict = {}

    if all_:
        if not yes and not dry_run and not is_json_mode():
            confirm = typer.confirm(
                f"Wipe ~/.taskmux/ entirely (keep {globalDaemonLogPath().parent}/config.toml)?",
                default=False,
            )
            if not confirm:
                console.print("Aborted")
                return
        reports["all"] = cleanAll(dry_run=dry_run, force=force)
    else:
        cli = TaskmuxCLI()
        proj = cli.config.name
        wt = cli.identity.worktree_id
        pid = cli.project_id

        if not scoped:
            if not yes and not dry_run and not is_json_mode():
                confirm = typer.confirm(
                    f"Wipe state for project '{pid}' (logs, state.json, certs, registry)?",
                    default=False,
                )
                if not confirm:
                    console.print("Aborted")
                    return
            reports["project"] = cleanProjectState(proj, wt, pid, dry_run=dry_run, force=force)
        else:
            if logs:
                reports["logs"] = cleanLogs(proj, wt, dry_run=dry_run)
            if events:
                reports["events"] = cleanEvents(dry_run=dry_run)
            if certs:
                reports["certs"] = cleanCerts(pid, dry_run=dry_run)

    if is_json_mode():
        print_result({"ok": True, "dry_run": dry_run, "reports": reports})
        return

    for scope, rep in reports.items():
        prefix = "[dim]would delete[/dim]" if dry_run else "Deleted"
        for path in rep["deleted"]:
            console.print(f"{prefix} {path}  [dim]({scope})[/dim]")
        for s in rep["skipped"]:
            console.print(f"[yellow]Skipped: {s}[/yellow]")
        for sess in rep.get("unregistered", []):
            verb = "would unregister" if dry_run else "Unregistered"
            console.print(f"{verb} '{sess}'  [dim]({scope})[/dim]")
    if not any(r["deleted"] or r["skipped"] or r.get("unregistered") for r in reports.values()):
        console.print("Nothing to clean")


@app.command()
def prune(
    apply: bool = typer.Option(  # noqa: B008
        False, "--apply", help="Act on the orphans (default is dry-run / report only)"
    ),
):
    """Detect (and optionally clean) orphaned tmux sessions, registry entries,
    leaked ports, and stale state.json windows.

    Default is a read-only report. Use --apply to kill leaked-port pids,
    drop stale registry rows, trim state.json, and kill stray tmux sessions.
    """
    from .cleanup import applyPrune, findOrphans

    report = findOrphans()

    if apply:
        actions = applyPrune(report)
        if is_json_mode():
            print_result({"ok": True, "report": report, "actions": actions})
            return
        if actions["killed_pids"]:
            console.print(f"Killed pids: {actions['killed_pids']}")
        for sess in actions["unregistered"]:
            console.print(f"Unregistered '{sess}'")
        for trim in actions["trimmed_state"]:
            console.print(f"Trimmed state for '{trim['session']}': {trim['tasks']}")
        for sess in actions["killed_sessions"]:
            console.print(f"Killed tmux session '{sess}'")
        if actions["removed_pidfile"]:
            console.print("Removed stale daemon.pid")
        if (
            not any(actions[k] for k in ("killed_pids", "unregistered", "killed_sessions"))
            and not actions["trimmed_state"]
            and not actions["removed_pidfile"]
        ):
            console.print("Nothing to prune")
        return

    if is_json_mode():
        print_result({"ok": True, "report": report, "applied": False})
        return

    any_found = False
    for sess in report["stray_tmux_sessions"]:
        any_found = True
        console.print(f"[yellow]stray tmux session:[/yellow] {sess}")
    for stale in report["stale_registry"]:
        any_found = True
        console.print(
            f"[yellow]stale registry:[/yellow] {stale['session']} ([dim]{stale['reason']}[/dim])"
        )
    for leak in report["leaked_ports"]:
        any_found = True
        console.print(
            f"[yellow]leaked port:[/yellow] {leak['session']}/{leak['task']} "
            f"port {leak['port']} held by pid {leak['pid']} ([dim]{leak['reason']}[/dim])"
        )
    for miss in report["missing_windows"]:
        any_found = True
        console.print(
            f"[yellow]stale state:[/yellow] {miss['session']}/{miss['task']} "
            f"port {miss['port']} (window gone)"
        )
    for d in report["orphan_log_dirs"]:
        any_found = True
        console.print(f"[yellow]orphan log dir:[/yellow] {d}")
    if report["stale_daemon_pid"] is not None:
        any_found = True
        console.print(f"[yellow]stale daemon.pid:[/yellow] {report['stale_daemon_pid']}")
    if not any_found:
        console.print("No orphans found")
    else:
        console.print("\n[dim]Re-run with --apply to clean up.[/dim]")


@app.command()
def inspect(
    task: str = typer.Argument(..., help="Task name to inspect"),
):
    """Inspect task state as JSON."""
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)
    data = _call_session("inspect", cli.project_id, task=task)
    if is_json_mode():
        print_result(data)
    else:
        console.print_json(json.dumps(data, default=str))


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
    rewrote = _reinjectAgentBlock()
    if is_json_mode():
        print_result(
            {
                "ok": True,
                "task": task,
                "command": command,
                "action": "added",
                "agent_files_rewritten": [str(p) for p in rewrote],
            }
        )
    else:
        console.print(f"Added task '{task}': {command}")
        for p in rewrote:
            console.print(f"  Updated {p.name}", style="dim")


@app.command()
def remove(
    task: str = typer.Argument(..., help="Task name to remove"),
):
    """Remove a task from taskmux.toml (kills it first if running)."""
    cli = TaskmuxCLI()
    if ipc_client.is_daemon_running():
        with contextlib.suppress(Exception):
            ipc_client.call("kill", params={"session": cli.project_id, "task": task}, ensure=False)
    _, removed = removeTask(None, task)
    rewrote = _reinjectAgentBlock() if removed else []
    if is_json_mode():
        print_result(
            {
                "ok": removed,
                "task": task,
                "action": "removed",
                "agent_files_rewritten": [str(p) for p in rewrote],
            }
        )
    elif removed:
        console.print(f"Removed task '{task}'")
        for p in rewrote:
            console.print(f"  Updated {p.name}", style="dim")
    else:
        console.print(f"Task '{task}' not found in config", style="red")


def _status():
    """Show session and task status."""
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)
    resp = ipc_client.call("list_tasks", params={"session": cli.project_id})
    data = resp.get("data", {})
    daemon_pid = get_daemon_pid()
    data["daemon_pid"] = daemon_pid

    if is_json_mode():
        print_result(data)
        return

    from .models import RestartPolicy

    session = data.get("session", cli.project_id)
    running = data.get("running", False)
    console.print(f"Session '{session}': {'Running' if running else 'Stopped'}")
    if running:
        console.print(f"Active tasks: {data.get('active_tasks', 0)}")

    if daemon_pid:
        console.print(f"Auto-restart: active (pid {daemon_pid})", style="green")
    else:
        any_restart = any(
            t.get("restart_policy") and t["restart_policy"] != str(RestartPolicy.NO)
            for t in data.get("tasks", [])
        )
        if any_restart:
            console.print(
                "Auto-restart: inactive — daemon offline",
                style="yellow",
            )

    proxy = data.get("proxy")
    if proxy and not proxy.get("bound"):
        console.print(f"Proxy: {proxy['reason']}", style="yellow")

    console.print("-" * 70)

    aliases = data.get("aliases") or []
    tasks = data.get("tasks", [])
    if not tasks and not aliases:
        console.print("No tasks configured")
        return
    if not tasks:
        console.print("No tasks configured")
        _print_alias_section(aliases)
        return

    has_public = any(t.get("public_url") for t in tasks)

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("Status", style="cyan", no_wrap=True)
    table.add_column("Task", style="magenta", no_wrap=True)
    table.add_column("URL", no_wrap=True)
    if has_public:
        table.add_column("Public URL", no_wrap=True)
    table.add_column("Command", overflow="ellipsis", no_wrap=True)
    table.add_column("Notes", style="dim", no_wrap=True)

    fail_rows: list[tuple[str, str, str]] = []
    for t in tasks:
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
        if t.get("tunnel"):
            notes.append(f"tunnel={t['tunnel']}")
        row = [
            f"[{icon_style}]{health_icon}[/{icon_style}]",
            status_text,
            t["name"],
            t.get("url") or "",
        ]
        if has_public:
            row.append(t.get("public_url") or "")
        row.extend([t["command"], " ".join(notes)])
        table.add_row(*row)
        last = t.get("last_health")
        if last and not last.get("ok") and last.get("reason"):
            fail_rows.append((t["name"], last.get("method", ""), last.get("reason", "")))

    console.print(table)
    for name, method, reason in fail_rows:
        console.print(f"    {name} fail: {method} — {reason}", style="red")

    _print_alias_section(aliases)


def _print_alias_section(aliases: list[dict]) -> None:
    """Render the 'Aliases (external routes)' section in human status output."""
    if not aliases:
        return
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
    """Check health of all tasks via the daemon."""
    cli = TaskmuxCLI()
    _ensure_session_known(cli.project_id, cli.config_path)

    healthy_count = 0
    total_count = len(cli.config.tasks)
    tasks_health: list[dict] = []
    for task_name in cli.config.tasks:
        resp = ipc_client.call("health", params={"session": cli.project_id, "task": task_name})
        result = resp.get("result", {})
        ok = bool(result.get("ok"))
        tasks_health.append(
            {
                "name": task_name,
                "healthy": ok,
                "method": result.get("method", "none"),
                "reason": result.get("reason"),
            }
        )
        if ok:
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
    """Show recent lifecycle events."""
    from .events import queryEvents
    from .supervisor import _parseSince

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
    task: str = typer.Argument(..., help="Task or alias name"),
):
    """Print the proxy URL for a task or alias: https://{host}.{project}.localhost"""
    from .aliases import lookupAlias
    from .url import taskUrl

    cli = TaskmuxCLI()
    cfg = cli.config.tasks.get(task)
    host: str | None = cfg.host if cfg is not None else None
    if host is None:
        alias = lookupAlias(cli.config.name, cli.identity.worktree_id, task)
        if alias is not None:
            host = alias["host"]
    if host is None:
        if cfg is None:
            err = "task_not_found"
            msg = f"'{task}' not found as task or alias"
        else:
            err = "no_host"
            msg = f"Task '{task}' has no host set (not exposed via proxy)"
        if is_json_mode():
            print_result({"ok": False, "error": err, "task": task})
        else:
            console.print(msg, style="yellow" if err == "no_host" else "red")
        sys.exit(1)
    u = taskUrl(cli.project_id, host)
    public_url: str | None = None
    if cfg is not None and cfg.public_hostname:
        public_url = f"https://{cfg.public_hostname}/"
    if is_json_mode():
        out: dict = {"ok": True, "task": task, "url": u}
        if public_url:
            out["public_url"] = public_url
        print_result(out)
    else:
        console.print(u)
        from .shell_env import clientTrustMissing

        if clientTrustMissing():
            console.print(
                "Tip: Node/Python may reject this cert — run 'taskmux ca trust-clients' once.",
                style="dim",
            )
        if public_url:
            console.print(f"public: {public_url}", style="cyan")


@app.command()
def watch():
    """Watch taskmux.toml for changes and reload on edit (foreground, no daemon)."""
    cli = TaskmuxCLI()
    watcher = SimpleConfigWatcher(cli)
    watcher.watch_config()


# ---------------------------------------------------------------------------
# Daemon sub-app
# ---------------------------------------------------------------------------


daemon_app = typer.Typer(
    name="daemon",
    help=(
        "Daemon lifecycle: start, stop, status, restart.\n\n"
        "Bare 'taskmux daemon' runs a foreground daemon. Use 'daemon start' to spawn detached."
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
    """Run a foreground daemon when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    _warn_unprivileged_daemon()
    d = TaskmuxDaemon(api_port=port)
    asyncio.run(d.start())


def _wait_for_pid_exit(pid: int, timeout: float = 5.0) -> bool:
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
    """Spawn the global detached daemon (idempotent)."""
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
    """Print the daemon PID. Exits 1 if no daemon is running."""
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
    table.add_column("State", justify="left")
    table.add_column("Tasks", justify="right")
    table.add_column("Config")
    for entry in registered:
        info = live.get(entry["session"], {})
        state = info.get("state", "[dim]unmanaged[/dim]" if pid is None else "ok")
        task_count = info.get("task_count", "?")
        table.add_row(
            entry["session"],
            str(state),
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
    """Add a project to the registry. Daemon (if running) picks it up live."""
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
    resp = ipc_client.call_no_ensure("list_projects", port=port, timeout=timeout)
    if resp is None:
        return {}
    projects = resp.get("projects", [])
    return {p["session"]: p for p in projects}


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
        console.print(
            "Tip: run 'taskmux ca trust-clients' so Node/Python "
            "(Claude Code, Cursor, etc.) trust this CA.",
            style="dim",
        )


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


@ca_app.command("trust-clients")
def ca_trust_clients(
    shell: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--shell",
        help="Override $SHELL (zsh|bash|fish).",
    ),
    print_only: bool = typer.Option(
        False,
        "--print",
        help="Print exports to stdout, do not write any file.",
    ),
):
    """Trust the mkcert root CA in Node.js and Python by writing env-var
    exports into your shell rc file (NODE_EXTRA_CA_CERTS, REQUESTS_CA_BUNDLE,
    SSL_CERT_FILE).
    """
    from . import ca, shell_env

    try:
        sh = shell_env.detectShell(shell)
        mkcertPath = ca.caRootPath()
        bundlePath = ca.buildCombinedBundle(mkcertPath)
    except (ca.MkcertMissing, TaskmuxError) as e:
        msg = e.message if hasattr(e, "message") else str(e)
        if is_json_mode():
            print_result({"ok": False, "error": msg})
        else:
            console.print(msg, style="red")
        sys.exit(1)

    if print_only:
        exports = shell_env.renderExportsOnly(bundlePath, sh)
        if is_json_mode():
            print_result(
                {
                    "ok": True,
                    "action": "printed",
                    "shell": sh,
                    "caPath": str(bundlePath),
                    "mkcertCaPath": str(mkcertPath),
                    "exports": exports,
                }
            )
        else:
            sys.stdout.write(exports)
            sys.stdout.flush()
        return

    result = shell_env.applyTrustClients(bundlePath, sh)
    result["mkcertCaPath"] = str(mkcertPath)
    if not result.get("ok"):
        if is_json_mode():
            print_result(result)
        else:
            console.print(result.get("error", "trust-clients failed"), style="red")
        sys.exit(1)

    if is_json_mode():
        print_result(result)
    else:
        action = result["action"]
        rc = result["rcFile"]
        if action == "unchanged":
            console.print(f"No change — exports already present in {rc}.")
        else:
            verb = "Replaced" if action == "replaced" else "Wrote"
            console.print(f"{verb} 3 exports in {rc}.")
            console.print(f"To apply now: source {rc}")
            if sh == "zsh":
                console.print(
                    "New shells and macOS GUI app launches inherit it automatically.",
                    style="dim",
                )
            elif sh == "bash":
                console.print(
                    "New shells inherit it; relaunch GUI apps so they pick it up.",
                    style="dim",
                )


app.add_typer(ca_app)


# ---------------------------------------------------------------------------
# DNS sub-app
# ---------------------------------------------------------------------------

dns_app = typer.Typer(
    name="dns",
    help="Manage the in-process DNS server delegation (host_resolver = 'dns_server').",
    no_args_is_help=True,
)


@dns_app.command("install")
def dns_install_cmd():
    """Install OS-level DNS delegation for the configured TLD."""
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
# Worktree sub-app — inspect repo/worktree-scoped sessions
# ---------------------------------------------------------------------------

worktree_app = typer.Typer(
    name="worktree",
    help="Inspect git-worktree-scoped sessions for the current repo.",
    no_args_is_help=True,
)


def _worktreeRowsForRepo(primary_path: Path | None) -> list[dict]:
    """Cross-reference registry entries against a repo's primary worktree path."""
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
    from .shell_env import clientTrustMissing

    if clientTrustMissing():
        console.print(
            "Tip: Node/Python may reject these certs — run 'taskmux ca trust-clients' once.",
            style="dim",
        )


app.add_typer(worktree_app)


# ---------------------------------------------------------------------------
# Alias sub-app — register external ports as proxy routes (no tmux task)
# ---------------------------------------------------------------------------

alias_app = typer.Typer(
    name="alias",
    help=(
        "Register external ports as proxy routes (Docker containers, external "
        "dev servers). Aliases live in per-project aliases.json, separate "
        "from tasks in taskmux.toml."
    ),
    no_args_is_help=True,
)


@alias_app.command("add")
def alias_add(
    name: str = typer.Argument(..., help="Alias name (also default subdomain)"),
    port: int = typer.Argument(..., help="Target port on 127.0.0.1"),
    host: str | None = typer.Option(  # noqa: B008
        None, "--host", help="Override subdomain (defaults to alias name)"
    ),
):
    """Add a proxy alias: https://{host}.{project}.localhost → 127.0.0.1:{port}.

    The target server must already be running; taskmux does not start or
    monitor it. Conflicts with task `host` slugs are rejected at registration
    time.
    """
    from .aliases import addAlias

    cli = TaskmuxCLI()
    effective_host = host or name
    for task_name, task_cfg in cli.config.tasks.items():
        if task_cfg.host == effective_host:
            err = f"alias host '{effective_host}' collides with task '{task_name}' in taskmux.toml"
            if is_json_mode():
                print_result({"ok": False, "error": err})
            else:
                console.print(err, style="red")
            sys.exit(1)
    entry = addAlias(cli.config.name, cli.identity.worktree_id, name, port, host=host)
    try:
        registerProject(cli.project_id, cli.config_path)
    except TaskmuxError as e:
        if not is_json_mode():
            console.print(f"[yellow]Auto-register skipped:[/yellow] {e.message}")
    _notify_daemon_resync(cli.project_id)
    from .url import taskUrl

    u = taskUrl(cli.project_id, entry["host"])
    if is_json_mode():
        print_result(
            {
                "ok": True,
                "alias": name,
                "host": entry["host"],
                "port": entry["port"],
                "url": u,
            }
        )
    else:
        console.print(f"Alias '{name}' → {u} (127.0.0.1:{entry['port']})")
        from .shell_env import clientTrustMissing

        if clientTrustMissing():
            console.print(
                "Tip: Node/Python may reject this cert — run 'taskmux ca trust-clients' once.",
                style="dim",
            )


@alias_app.command("list")
def alias_list():
    """List all aliases for the current project."""
    from .aliases import loadAliases
    from .url import taskUrl

    cli = TaskmuxCLI()
    aliases = loadAliases(cli.config.name, cli.identity.worktree_id)
    rows = [
        {"name": n, "host": e["host"], "port": e["port"], "url": taskUrl(cli.project_id, e["host"])}
        for n, e in sorted(aliases.items())
    ]
    if is_json_mode():
        print_result({"aliases": rows, "count": len(rows)})
        return
    if not rows:
        console.print("No aliases configured")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Host")
    table.add_column("Port", justify="right")
    table.add_column("URL")
    for r in rows:
        table.add_row(r["name"], r["host"], str(r["port"]), r["url"])
    console.print(table)


@alias_app.command("remove")
def alias_remove(
    name: str = typer.Argument(..., help="Alias name"),
):
    """Remove an alias from the current project."""
    from .aliases import removeAlias

    cli = TaskmuxCLI()
    removed = removeAlias(cli.config.name, cli.identity.worktree_id, name)
    if not removed:
        if is_json_mode():
            print_result({"ok": False, "error": "alias_not_found", "alias": name})
        else:
            console.print(f"Alias '{name}' not found", style="yellow")
        sys.exit(1)
    _notify_daemon_resync(cli.project_id)
    if is_json_mode():
        print_result({"ok": True, "alias": name, "action": "removed"})
    else:
        console.print(f"Removed alias '{name}'")


app.add_typer(alias_app)


# ---------------------------------------------------------------------------
# Tunnel sub-app
# ---------------------------------------------------------------------------

tunnel_app = typer.Typer(
    name="tunnel",
    help=(
        'Inspect public-tunnel backends. Per-task `tunnel = "cloudflare"` '
        "in taskmux.toml exposes the service via a Cloudflare Tunnel; this "
        "command surfaces backend health and recent log lines."
    ),
    no_args_is_help=True,
)


@tunnel_app.command("status")
def tunnel_status_cmd():
    """Show health + last-sync state of every active tunnel backend."""
    resp = ipc_client.call("tunnel_status")
    entries = resp.get("tunnels", [])
    if is_json_mode():
        print_result({"ok": True, "tunnels": entries})
        return
    if not entries:
        console.print("No tunnels active.")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("Project", style="magenta", no_wrap=True)
    table.add_column("Backend", style="cyan", no_wrap=True)
    table.add_column("Tunnel", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Mappings", no_wrap=True)
    table.add_column("Note", style="dim")
    for e in entries:
        status_text = "ok" if e.get("last_sync_ok") else "error"
        status_style = "green" if e.get("last_sync_ok") else "red"
        running = e.get("cloudflared_running")
        if running is False:
            status_text = "stopped"
            status_style = "yellow"
        table.add_row(
            e.get("session", ""),
            e.get("backend", ""),
            e.get("tunnel_name") or "",
            f"[{status_style}]{status_text}[/{status_style}]",
            str(e.get("mappings", 0)),
            e.get("last_error") or "",
        )
    console.print(table)


@tunnel_app.command("logs")
def tunnel_logs_cmd(
    backend: str = typer.Argument("cloudflare", help="Backend name"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Tail the log file"),
    lines: int = typer.Option(50, "--lines", "-n", help="Lines of history"),
):
    """Tail the cloudflared log for a project's tunnel."""
    from .paths import tunnelStateDir

    log_dir = tunnelStateDir(backend)
    if not log_dir.exists():
        if is_json_mode():
            print_result({"ok": False, "error": "no_logs", "backend": backend})
        else:
            console.print(f"No logs for backend '{backend}' yet.", style="yellow")
        return
    log_files = sorted(log_dir.glob("*.log"))
    if not log_files:
        if is_json_mode():
            print_result({"ok": False, "error": "no_logs", "backend": backend})
        else:
            console.print(f"No logs for backend '{backend}' yet.", style="yellow")
        return
    if is_json_mode():
        print_result({"ok": True, "backend": backend, "files": [str(p) for p in log_files]})
        return
    if follow:
        if len(log_files) == 1:
            ipc_client.follow_log_file(log_files[0])
        else:
            tagged = [(p.stem, p, "cyan") for p in log_files]
            ipc_client.follow_log_files(tagged)
        return
    for path in log_files:
        console.print(f"==> {path} <==", style="dim")
        with path.open("rb") as f:
            data = f.read()
        text = data.decode("utf-8", errors="replace").splitlines()
        for line in text[-lines:]:
            console.print(line)


app.add_typer(tunnel_app)


def _hoist_global_flags(argv: list[str]) -> list[str]:
    """Move `--json` (and `-V`/`--version`) to before the first subcommand.

    Typer only recognizes app-level options before the subcommand
    (`taskmux --json daemon status`). Agents and humans naturally place flags
    after the subcommand (`taskmux daemon status --json`); without this hoist,
    Typer rejects them with `No such option`. Hoisting (rather than stripping)
    means the existing `main_callback` still sees the flag and `set_json_mode`
    runs through its single source of truth.

    Context-aware to avoid eating real argument values:
      - skip after a `--` end-of-options marker (everything after is data),
      - skip when the previous token is a known value-taking option (e.g.
        `--grep --json` filters logs for the literal pattern `--json`).
    """
    GLOBAL = {"--json", "-V", "--version"}
    # Long-and-short forms of every option in the CLI that takes a value.
    # If `--json` appears immediately after one of these, treat it as the
    # option's argument, not as a global flag.
    VALUE_TAKING = {
        "--grep",
        "-g",
        "--lines",
        "-n",
        "--context",
        "-C",
        "--since",
        "--task",
        "--limit",
        "--cwd",
        "--host",
        "--health-check",
        "--depends-on",
        "--port",
        "--config",
        "-c",
        "--type",
    }
    hoisted: list[str] = []
    rest: list[str] = []
    end_of_options = False
    prev: str | None = None
    for arg in argv:
        if end_of_options:
            rest.append(arg)
        elif arg == "--":
            end_of_options = True
            rest.append(arg)
        elif arg in GLOBAL and prev not in VALUE_TAKING:
            hoisted.append(arg)
        else:
            rest.append(arg)
        prev = arg
    return hoisted + rest


def main():
    """Main entry point for the CLI — global exception boundary."""
    with contextlib.suppress(Exception):
        migrateLayout()
    sys.argv = [sys.argv[0], *_hoist_global_flags(sys.argv[1:])]
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
