"""Centralized filesystem layout for taskmux state under ~/.taskmux/.

Layout:
  ~/.taskmux/
    daemon.pid                # GLOBAL — single multi-project daemon
    daemon.log                # GLOBAL
    events.jsonl              # global, cross-project
    registry.json             # central project registry
    projects/
      {project}/              # primary worktree (or non-worktree project)
        logs/
          {task}.log[.N]
        state.json
        worktrees/
          {worktree_id}/      # linked worktree
            logs/
              {task}.log[.N]
            state.json
    certs/
      {project_id}/           # cert per project_id (primary OR linked)
    .migrated-v2              # legacy layout migration marker
    .migrated-v3              # unified-daemon migration marker
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal as _sig
import time
from pathlib import Path

TASKMUX_DIR = Path.home() / ".taskmux"
EVENTS_FILE = TASKMUX_DIR / "events.jsonl"
PROJECTS_DIR = TASKMUX_DIR / "projects"
CERTS_DIR = TASKMUX_DIR / "certs"
REGISTRY_PATH = TASKMUX_DIR / "registry.json"
GLOBAL_DAEMON_PID = TASKMUX_DIR / "daemon.pid"
GLOBAL_DAEMON_LOG = TASKMUX_DIR / "daemon.log"
GLOBAL_CONFIG_PATH = TASKMUX_DIR / "config.toml"

_MIGRATION_MARKER = TASKMUX_DIR / ".migrated-v2"
_MIGRATION_MARKER_V3 = TASKMUX_DIR / ".migrated-v3"


def projectDir(project: str, worktree_id: str | None = None) -> Path:
    """Per-project (or per-worktree) state directory. Created on demand.

    Primary / non-worktree: ~/.taskmux/projects/{project}/
    Linked worktree:        ~/.taskmux/projects/{project}/worktrees/{worktree_id}/
    """
    if worktree_id:
        return PROJECTS_DIR / project / "worktrees" / worktree_id
    return PROJECTS_DIR / project


def projectWorktreesDir(project: str) -> Path:
    """Container for all linked-worktree state of a project."""
    return PROJECTS_DIR / project / "worktrees"


def projectLogsDir(project: str, worktree_id: str | None = None) -> Path:
    return projectDir(project, worktree_id) / "logs"


def taskLogPath(project: str, task: str, worktree_id: str | None = None) -> Path:
    return projectLogsDir(project, worktree_id) / f"{task}.log"


def projectStatePath(project: str, worktree_id: str | None = None) -> Path:
    """Per-project (or per-worktree) runtime state JSON (assigned ports, etc.)."""
    return projectDir(project, worktree_id) / "state.json"


def projectAliasesPath(project: str, worktree_id: str | None = None) -> Path:
    """Per-project alias map JSON. Aliases route external ports through the proxy."""
    return projectDir(project, worktree_id) / "aliases.json"


def projectCertDir(project_id: str) -> Path:
    """mkcert-issued cert directory keyed by project_id (already encodes worktree)."""
    return CERTS_DIR / project_id


def ensureProjectDir(project: str, worktree_id: str | None = None) -> Path:
    d = projectDir(project, worktree_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensureProjectCertDir(project_id: str) -> Path:
    d = projectCertDir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensureTaskmuxDir() -> Path:
    TASKMUX_DIR.mkdir(parents=True, exist_ok=True)
    return TASKMUX_DIR


def listProjects() -> list[tuple[str, str | None]]:
    """Return (project, worktree_id|None) tuples for everything on disk.

    The primary slot for each project (the directory itself, with `state.json`
    or `logs/` inside) yields `(project, None)`. Each subdir under
    `projects/{project}/worktrees/` yields `(project, worktree_id)`.
    """
    if not PROJECTS_DIR.exists():
        return []
    out: list[tuple[str, str | None]] = []
    for p in sorted(PROJECTS_DIR.iterdir()):
        if not p.is_dir():
            continue
        # Treat the project dir as primary if it has any non-`worktrees`
        # children — the legacy layout had logs/state directly inside.
        has_primary_state = any(child.name != "worktrees" for child in p.iterdir())
        if has_primary_state:
            out.append((p.name, None))
        wt_dir = p / "worktrees"
        if wt_dir.is_dir():
            for w in sorted(wt_dir.iterdir()):
                if w.is_dir():
                    out.append((p.name, w.name))
    return out


def globalDaemonPidPath() -> Path:
    return GLOBAL_DAEMON_PID


def globalDaemonLogPath() -> Path:
    return GLOBAL_DAEMON_LOG


def registryPath() -> Path:
    return REGISTRY_PATH


def globalConfigPath() -> Path:
    return GLOBAL_CONFIG_PATH


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def migrate() -> dict:
    """Idempotent migration of ~/.taskmux/ layout.

    v2 (legacy → per-project):
      ~/.taskmux/logs/{session}/   → ~/.taskmux/projects/{session}/logs/
      ~/.taskmux/{daemon.pid,daemon.log} → deleted (replaced)

    v3 (per-project → unified-daemon):
      ~/.taskmux/projects/{session}/{daemon.pid,daemon.log} → SIGTERM + delete
    """
    summary: dict = {"v2": False, "v3": False, "sessions": [], "removed": [], "stopped": []}

    ensureTaskmuxDir()

    if not _MIGRATION_MARKER.exists():
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        old_logs = TASKMUX_DIR / "logs"
        if old_logs.exists() and old_logs.is_dir():
            for session_dir in old_logs.iterdir():
                if not session_dir.is_dir():
                    continue
                dest = projectLogsDir(session_dir.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    for f in session_dir.iterdir():
                        target = dest / f.name
                        if not target.exists():
                            shutil.move(str(f), str(target))
                    with contextlib.suppress(OSError):
                        session_dir.rmdir()
                else:
                    shutil.move(str(session_dir), str(dest))
                summary["sessions"].append(session_dir.name)
            with contextlib.suppress(OSError):
                old_logs.rmdir()

        for legacy in ("daemon.pid", "daemon.log"):
            p = TASKMUX_DIR / legacy
            if p.exists():
                with contextlib.suppress(OSError):
                    p.unlink()
                summary["removed"].append(legacy)
        _MIGRATION_MARKER.touch()
        summary["v2"] = True

    if not _MIGRATION_MARKER_V3.exists():
        if PROJECTS_DIR.exists():
            for session_dir in PROJECTS_DIR.iterdir():
                if not session_dir.is_dir():
                    continue
                pid_file = session_dir / "daemon.pid"
                log_file = session_dir / "daemon.log"
                if pid_file.exists():
                    try:
                        pid = int(pid_file.read_text().strip())
                        try:
                            os.kill(pid, 0)
                            os.kill(pid, _sig.SIGTERM)
                            _wait_for_pid_exit(pid, timeout=5.0)
                            summary["stopped"].append({"session": session_dir.name, "pid": pid})
                        except OSError:
                            pass
                    except (ValueError, OSError):
                        pass
                    with contextlib.suppress(OSError):
                        pid_file.unlink()
                if log_file.exists():
                    with contextlib.suppress(OSError):
                        log_file.unlink()
        _MIGRATION_MARKER_V3.touch()
        summary["v3"] = True

    return summary
