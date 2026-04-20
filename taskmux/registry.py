"""Central project registry for the unified taskmux daemon.

Stores `~/.taskmux/registry.json`:

    {
      "projects": {
        "myapp": {"session": "myapp", "config_path": "/abs/.../taskmux.toml",
                  "registered_at": "2026-04-24T10:00:00Z"},
        ...
      }
    }

Atomic writes (temp + os.replace) under fcntl.flock so concurrent CLI invocations
don't corrupt the file. Functional API — no class state.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .errors import ErrorCode, TaskmuxError
from .paths import ensureTaskmuxDir, registryPath


class RegistryEntry(TypedDict):
    session: str
    config_path: str
    registered_at: str


def _lockPath() -> Path:
    return registryPath().with_suffix(".lock")


def _withLock(write: bool):  # type: ignore[misc]
    """Context-manager factory for an exclusive (write) or shared (read) flock."""
    import contextlib as _cl

    @_cl.contextmanager
    def _cm():
        ensureTaskmuxDir()
        lock_path = _lockPath()
        flag = os.O_CREAT | os.O_RDWR
        fd = os.open(lock_path, flag, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    return _cm()


def readRegistry() -> dict[str, RegistryEntry]:
    """Read all registry entries. Returns {} if missing or corrupt."""
    path = registryPath()
    if not path.exists():
        return {}
    with _withLock(write=False):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return {}
    out: dict[str, RegistryEntry] = {}
    for session, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        config_path = entry.get("config_path")
        if not isinstance(config_path, str):
            continue
        out[session] = RegistryEntry(
            session=session,
            config_path=config_path,
            registered_at=str(entry.get("registered_at", "")),
        )
    return out


def writeRegistry(entries: dict[str, RegistryEntry]) -> None:
    """Atomic write under exclusive lock."""
    ensureTaskmuxDir()
    path = registryPath()
    payload = {"projects": dict(entries)}
    with _withLock(write=True):
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=".registry-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, path)


def registerProject(session: str, config_path: Path | str) -> RegistryEntry:
    """Add (or refresh) a project in the registry.

    Idempotent on re-register of the same {session, config_path}.
    Raises SESSION_ALREADY_REGISTERED if `session` is already taken by a
    different config path.
    """
    abs_path = str(Path(config_path).expanduser().resolve())
    with _withLock(write=True):
        entries = _readUnlocked()
        existing = entries.get(session)
        if existing and existing["config_path"] != abs_path:
            raise TaskmuxError(
                ErrorCode.SESSION_ALREADY_REGISTERED,
                session=session,
                existing_path=existing["config_path"],
                new_path=abs_path,
            )
        if existing:
            return existing
        entry = RegistryEntry(
            session=session,
            config_path=abs_path,
            registered_at=datetime.now(UTC).isoformat(),
        )
        entries[session] = entry
        _writeUnlocked(entries)
        return entry


def unregisterProject(session: str) -> bool:
    """Remove a project. Returns True if it was present."""
    with _withLock(write=True):
        entries = _readUnlocked()
        if session not in entries:
            return False
        del entries[session]
        _writeUnlocked(entries)
        return True


def listRegistered() -> list[RegistryEntry]:
    """Return registry entries sorted by session name."""
    return sorted(readRegistry().values(), key=lambda e: e["session"])


def _readUnlocked() -> dict[str, RegistryEntry]:
    """Read without acquiring the lock — caller must hold it."""
    path = registryPath()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, dict):
        return {}
    out: dict[str, RegistryEntry] = {}
    for session, entry in projects.items():
        if not isinstance(entry, dict):
            continue
        config_path = entry.get("config_path")
        if not isinstance(config_path, str):
            continue
        out[session] = RegistryEntry(
            session=session,
            config_path=config_path,
            registered_at=str(entry.get("registered_at", "")),
        )
    return out


def _writeUnlocked(entries: dict[str, RegistryEntry]) -> None:
    """Write without acquiring the lock — caller must hold it."""
    path = registryPath()
    payload = {"projects": dict(entries)}
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=".registry-",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)
