"""Centralized filesystem layout for taskmux state under ~/.taskmux/.

Layout:
  ~/.taskmux/
    daemon.pid                # GLOBAL — single multi-project daemon
    daemon.log                # GLOBAL
    events.jsonl              # global, cross-project
    registry.json             # central project registry
    projects/
      {session_name}/
        logs/
          {task}.log[.N]
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
REGISTRY_PATH = TASKMUX_DIR / "registry.json"
GLOBAL_DAEMON_PID = TASKMUX_DIR / "daemon.pid"
GLOBAL_DAEMON_LOG = TASKMUX_DIR / "daemon.log"
GLOBAL_CONFIG_PATH = TASKMUX_DIR / "config.toml"

_MIGRATION_MARKER = TASKMUX_DIR / ".migrated-v2"
_MIGRATION_MARKER_V3 = TASKMUX_DIR / ".migrated-v3"


def projectDir(session: str) -> Path:
    """Per-project state directory. Created on demand."""
    return PROJECTS_DIR / session


def projectLogsDir(session: str) -> Path:
    return projectDir(session) / "logs"


def taskLogPath(session: str, task: str) -> Path:
    return projectLogsDir(session) / f"{task}.log"


def ensureProjectDir(session: str) -> Path:
    d = projectDir(session)
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensureTaskmuxDir() -> Path:
    TASKMUX_DIR.mkdir(parents=True, exist_ok=True)
    return TASKMUX_DIR


def listProjects() -> list[str]:
    """Return session names of projects with state on disk."""
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())


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
