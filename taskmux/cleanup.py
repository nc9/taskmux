"""State cleanup + orphan reaping for taskmux.

Two flavors:
  - `cleanProjectState` / `cleanLogs` / `cleanEvents` / `cleanCerts` / `cleanAll`:
    deliberate state wipes invoked by `taskmux clean`.
  - `findOrphans` / `applyPrune`: detect-and-reap leaked tmux sessions,
    stale registry entries, dangling state.json windows, leaked ports.
    Powers `taskmux prune`.

Pure functions; CLI layer formats results. Every action is reported as a
list of (kind, target) pairs so dry-run + JSON output are trivial.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import TypedDict

from . import paths
from .registry import readRegistry, unregisterProject


class CleanReport(TypedDict):
    deleted: list[str]
    skipped: list[str]
    unregistered: list[str]


class OrphanReport(TypedDict):
    stray_tmux_sessions: list[str]
    stale_registry: list[dict]
    leaked_ports: list[dict]
    missing_windows: list[dict]
    orphan_log_dirs: list[str]
    stale_daemon_pid: int | None


def _emptyClean() -> CleanReport:
    return {"deleted": [], "skipped": [], "unregistered": []}


def _rmTree(path: Path, report: CleanReport, dry_run: bool) -> None:
    if not path.exists():
        return
    if dry_run:
        report["deleted"].append(str(path))
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        report["deleted"].append(str(path))
    except OSError as e:
        report["skipped"].append(f"{path}: {e}")


def _projectIsRunning(project: str, worktree_id: str | None, project_id: str) -> bool:
    """True if a tmux session for this project_id still has windows."""
    try:
        import libtmux

        srv = libtmux.Server()
        sess = srv.sessions.get(session_name=project_id)
    except Exception:  # noqa: BLE001
        return False
    if sess is None:
        return False
    try:
        return len(list(sess.windows)) > 0
    except Exception:  # noqa: BLE001
        return False


def cleanProjectState(
    project: str,
    worktree_id: str | None,
    project_id: str,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> CleanReport:
    """Wipe per-project logs, state.json, aliases, certs; unregister from daemon.

    Refuses (skipped reason) when the project's tmux session has live windows
    unless `force=True`. When cleaning the primary slot, the linked-worktree
    `worktrees/` subdir is preserved — only this slot is wiped.
    """
    report = _emptyClean()
    if not force and _projectIsRunning(project, worktree_id, project_id):
        report["skipped"].append(
            f"{project_id}: session has live windows; pass --force to wipe anyway"
        )
        return report

    proj_dir = paths.projectDir(project, worktree_id)
    if proj_dir.exists():
        if worktree_id is None:
            for child in proj_dir.iterdir():
                if child.name == "worktrees":
                    continue
                _rmTree(child, report, dry_run)
        else:
            _rmTree(proj_dir, report, dry_run)

    cert_dir = paths.projectCertDir(project_id)
    _rmTree(cert_dir, report, dry_run)

    if not dry_run and unregisterProject(project_id) or dry_run and project_id in readRegistry():
        report["unregistered"].append(project_id)

    return report


def cleanLogs(
    project: str,
    worktree_id: str | None,
    task: str | None = None,
    *,
    dry_run: bool = False,
) -> CleanReport:
    """Delete persistent log files for a project (or one task within it)."""
    report = _emptyClean()
    log_dir = paths.projectLogsDir(project, worktree_id)
    if not log_dir.exists():
        return report
    if task:
        for f in log_dir.glob(f"{task}.log*"):
            _rmTree(f, report, dry_run)
    else:
        _rmTree(log_dir, report, dry_run)
    return report


def cleanEvents(*, dry_run: bool = False) -> CleanReport:
    """Truncate ~/.taskmux/events.jsonl."""
    report = _emptyClean()
    f = paths.EVENTS_FILE
    if not f.exists():
        return report
    if dry_run:
        report["deleted"].append(str(f))
        return report
    try:
        f.write_text("")
        report["deleted"].append(str(f))
    except OSError as e:
        report["skipped"].append(f"{f}: {e}")
    return report


def cleanCerts(project_id: str, *, dry_run: bool = False) -> CleanReport:
    """Remove minted *.localhost certs for one project (mkcert root CA stays)."""
    report = _emptyClean()
    _rmTree(paths.projectCertDir(project_id), report, dry_run)
    return report


def cleanAll(*, dry_run: bool = False, force: bool = False) -> CleanReport:
    """Wipe ~/.taskmux/ entirely except config.toml.

    Refuses when the daemon is running (would orphan it) unless force=True.
    """
    from .daemon import get_daemon_pid

    report = _emptyClean()
    if not force and get_daemon_pid() is not None:
        report["skipped"].append(
            "daemon is running; stop it (`taskmux daemon stop`) or pass --force"
        )
        return report
    root = paths.TASKMUX_DIR
    if not root.exists():
        return report
    keep = {paths.GLOBAL_CONFIG_PATH.name}
    for child in root.iterdir():
        if child.name in keep:
            continue
        _rmTree(child, report, dry_run)
    return report


def _portHolder(port: int) -> int | None:
    """Return PID listening on `port`, or None if nothing or lsof unavailable."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    pid_str = result.stdout.strip().splitlines()
    if not pid_str:
        return None
    try:
        return int(pid_str[0])
    except ValueError:
        return None


def _readAssignedPorts(project: str, worktree_id: str | None) -> dict[str, int]:
    p = paths.projectStatePath(project, worktree_id)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("assigned_ports", {}) if isinstance(data, dict) else {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _liveWindows(project_id: str) -> set[str] | None:
    """Set of window names in the live tmux session, or None if no session."""
    try:
        import libtmux

        srv = libtmux.Server()
        sess = srv.sessions.get(session_name=project_id)
    except Exception:  # noqa: BLE001
        return None
    if sess is None:
        return None
    try:
        return {w.window_name for w in sess.windows if w.window_name}
    except Exception:  # noqa: BLE001
        return None


def _liveTmuxSessions() -> set[str]:
    try:
        import libtmux

        srv = libtmux.Server()
        return {s.session_name for s in srv.sessions if s.session_name}
    except Exception:  # noqa: BLE001
        return set()


def findOrphans() -> OrphanReport:
    """Scan registry / state.json / tmux for orphaned entries. Read-only."""
    from .config import loadProjectIdentity
    from .daemon import get_daemon_pid

    report: OrphanReport = {
        "stray_tmux_sessions": [],
        "stale_registry": [],
        "leaked_ports": [],
        "missing_windows": [],
        "orphan_log_dirs": [],
        "stale_daemon_pid": None,
    }

    registry = readRegistry()
    registered_ids: set[str] = set()

    for session, entry in registry.items():
        cfg_path = Path(entry["config_path"])
        if not cfg_path.exists():
            report["stale_registry"].append(
                {"session": session, "config_path": str(cfg_path), "reason": "config missing"}
            )
            continue
        try:
            ident = loadProjectIdentity(cfg_path)
        except Exception as e:  # noqa: BLE001
            report["stale_registry"].append(
                {"session": session, "config_path": str(cfg_path), "reason": f"load failed: {e}"}
            )
            continue
        registered_ids.add(ident.project_id)

        ports = _readAssignedPorts(ident.project, ident.worktree_id)
        windows = _liveWindows(ident.project_id)
        for task_name, port in ports.items():
            if windows is None:
                holder = _portHolder(port)
                if holder is not None:
                    report["leaked_ports"].append(
                        {
                            "session": session,
                            "project_id": ident.project_id,
                            "task": task_name,
                            "port": port,
                            "pid": holder,
                            "reason": "no tmux session",
                        }
                    )
                continue
            if task_name not in windows:
                report["missing_windows"].append(
                    {
                        "session": session,
                        "project_id": ident.project_id,
                        "task": task_name,
                        "port": port,
                    }
                )
                holder = _portHolder(port)
                if holder is not None:
                    report["leaked_ports"].append(
                        {
                            "session": session,
                            "project_id": ident.project_id,
                            "task": task_name,
                            "port": port,
                            "pid": holder,
                            "reason": "window gone",
                        }
                    )

    live_sessions = _liveTmuxSessions()
    for sess_name in live_sessions:
        if sess_name in registered_ids:
            continue
        if (paths.PROJECTS_DIR / sess_name).exists():
            report["stray_tmux_sessions"].append(sess_name)

    on_disk_ids: set[str] = set()
    for project, worktree_id in paths.listProjects():
        if worktree_id is None:
            on_disk_ids.add(project)
        else:
            on_disk_ids.add(f"{project}-{worktree_id}")
    for pid_id in on_disk_ids - registered_ids:
        if pid_id in live_sessions:
            continue
        report["orphan_log_dirs"].append(pid_id)

    pidfile = paths.GLOBAL_DAEMON_PID
    if pidfile.exists() and get_daemon_pid() is None:
        try:
            report["stale_daemon_pid"] = int(pidfile.read_text().strip())
        except (OSError, ValueError):
            report["stale_daemon_pid"] = -1

    return report


def applyPrune(report: OrphanReport) -> dict:
    """Act on a prune report: kill leaked-port pids, drop stale registry rows,
    rewrite state.json without missing-window ports, kill stray tmux sessions,
    drop stale daemon.pid. Returns a summary of actions taken.
    """
    actions: dict = {
        "killed_pids": [],
        "unregistered": [],
        "trimmed_state": [],
        "killed_sessions": [],
        "removed_pidfile": False,
    }

    for leak in report["leaked_ports"]:
        pid = leak.get("pid")
        if not isinstance(pid, int):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            actions["killed_pids"].append(pid)
        except OSError:
            continue

    for stale in report["stale_registry"]:
        sess = stale.get("session")
        if isinstance(sess, str) and unregisterProject(sess):
            actions["unregistered"].append(sess)

    by_project: dict[str, list[str]] = {}
    for miss in report["missing_windows"]:
        sess = miss.get("session")
        task = miss.get("task")
        if isinstance(sess, str) and isinstance(task, str):
            by_project.setdefault(sess, []).append(task)
    if by_project:
        from .config import loadProjectIdentity

        registry = readRegistry()
        for sess, tasks in by_project.items():
            entry = registry.get(sess)
            if not entry:
                continue
            cfg_path = Path(entry["config_path"])
            if not cfg_path.exists():
                continue
            try:
                ident = loadProjectIdentity(cfg_path)
            except Exception:  # noqa: BLE001
                continue
            state_path = paths.projectStatePath(ident.project, ident.worktree_id)
            if not state_path.exists():
                continue
            try:
                data = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            ports = data.get("assigned_ports", {}) if isinstance(data, dict) else {}
            removed = [t for t in tasks if t in ports]
            for t in removed:
                ports.pop(t, None)
            if removed:
                data["assigned_ports"] = ports
                with contextlib.suppress(OSError):
                    state_path.write_text(json.dumps(data, indent=2))
                actions["trimmed_state"].append({"session": sess, "tasks": removed})

    if report["stray_tmux_sessions"]:
        try:
            import libtmux

            srv = libtmux.Server()
            for sess_name in report["stray_tmux_sessions"]:
                with contextlib.suppress(Exception):
                    s = srv.sessions.get(session_name=sess_name)
                    if s is not None:
                        s.kill()
                        actions["killed_sessions"].append(sess_name)
        except Exception:  # noqa: BLE001
            pass

    if report["stale_daemon_pid"] is not None:
        with contextlib.suppress(OSError):
            paths.GLOBAL_DAEMON_PID.unlink()
            actions["removed_pidfile"] = True

    return actions
