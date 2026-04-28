"""Per-project alias map.

An alias maps a name to an `(host, port)` pair routed through the daemon
proxy without a corresponding tmux task. Used for Docker containers,
external dev servers, and anything else taskmux doesn't itself launch.

Storage: ~/.taskmux/projects/{project}/[worktrees/{worktree_id}/]aliases.json

    {"db": {"host": "db", "port": 5432}, ...}

Functional API. Pure dict in / dict out — no class state, no daemon coupling.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import TypedDict

from .errors import ErrorCode, TaskmuxError
from .paths import ensureProjectDir, projectAliasesPath


class AliasEntry(TypedDict):
    host: str
    port: int


def _readUnchecked(path: Path) -> dict[str, AliasEntry]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, AliasEntry] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        port = entry.get("port")
        if not isinstance(host, str) or not isinstance(port, int):
            continue
        out[name] = AliasEntry(host=host, port=port)
    return out


def loadAliases(project: str, worktree_id: str | None = None) -> dict[str, AliasEntry]:
    """Read all aliases for a project (or worktree). Empty dict if missing."""
    return _readUnchecked(projectAliasesPath(project, worktree_id))


def _writeAtomic(path: Path, aliases: dict[str, AliasEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".aliases-", suffix=".tmp", delete=False
    ) as tmp:
        json.dump(dict(aliases), tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def saveAliases(project: str, worktree_id: str | None, aliases: dict[str, AliasEntry]) -> None:
    ensureProjectDir(project, worktree_id)
    _writeAtomic(projectAliasesPath(project, worktree_id), aliases)


def addAlias(
    project: str,
    worktree_id: str | None,
    name: str,
    port: int,
    host: str | None = None,
) -> AliasEntry:
    """Add or replace an alias. `host` defaults to `name`."""
    if not name or "." in name or "/" in name:
        raise TaskmuxError(
            ErrorCode.INVALID_ARGUMENT,
            detail=f"alias name must be a simple slug, got {name!r}",
        )
    effective_host = host if host is not None else name
    if effective_host in ("*", "@"):
        raise TaskmuxError(
            ErrorCode.INVALID_ARGUMENT,
            detail=f"alias host {effective_host!r} reserved for tasks; pick a slug",
        )
    if not (1 <= port <= 65535):
        raise TaskmuxError(ErrorCode.INVALID_ARGUMENT, detail=f"port out of range: {port}")
    aliases = loadAliases(project, worktree_id)
    for other_name, other_entry in aliases.items():
        if other_name == name:
            continue
        if other_entry["host"] == effective_host:
            raise TaskmuxError(
                ErrorCode.INVALID_ARGUMENT,
                detail=(
                    f"alias host {effective_host!r} already used by alias "
                    f"{other_name!r} (proxy routes are keyed by host)"
                ),
            )
    entry = AliasEntry(host=effective_host, port=port)
    aliases[name] = entry
    saveAliases(project, worktree_id, aliases)
    return entry


def removeAlias(project: str, worktree_id: str | None, name: str) -> bool:
    aliases = loadAliases(project, worktree_id)
    if name not in aliases:
        return False
    del aliases[name]
    if aliases:
        saveAliases(project, worktree_id, aliases)
    else:
        with contextlib.suppress(OSError):
            projectAliasesPath(project, worktree_id).unlink()
    return True


def lookupAlias(project: str, worktree_id: str | None, name: str) -> AliasEntry | None:
    return loadAliases(project, worktree_id).get(name)
