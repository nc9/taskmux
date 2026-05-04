"""Per-client MCP config installers.

Each function returns a structured result so the CLI can render either human
or JSON output. Config writes are atomic (temp file + rename); existing
entries for other servers are preserved.

Supported clients (v1):
  * `claude`         — `~/.claude/settings.json`            (user-global)
  * `claude-project` — `<project>/.mcp.json`                (project-shared)
  * `cursor`         — `~/.cursor/mcp.json`                 (user-global)
  * `codex`          — `~/.codex/config.toml`               (user-global)
  * `codex-project`  — `<project>/.codex/config.toml`       (project-shared)
  * `continue`       — `~/.continue/config.json`            (user-global)

Notes:
  * `claude-project` writes `.mcp.json` at the repo root — Claude Code
    only honors that file for project MCP servers (NOT
    `.claude/settings.json`, which is for hooks/permissions/env).
  * `codex-project` writes `.codex/config.toml` at the repo root. Codex
    CLI resolves config in this order: CLI flags → profile → project
    `.codex/config.toml` (closest cwd ancestor wins; trusted projects
    only) → user `~/.codex/config.toml`. So a per-project taskmux pin
    coexists cleanly with any user-global servers (chrome-devtools etc.).

Cline + Goose deliberately skipped — their config layouts are more invasive
(VS Code settings JSON / YAML extension list); add later on demand.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit

ALL_CLIENTS = (
    "claude",
    "claude-project",
    "cursor",
    "codex",
    "codex-project",
    "continue",
)


def serverUrl(api_port: int, path: str = "/mcp", session: str | None = None) -> str:
    """Daemon URL for the MCP endpoint.

    When `session` is provided, append `?session=<name>` so the daemon
    pins this connection to that project. Trailing slash on the path
    sidesteps the 307 redirect FastMCP issues for bare `/mcp` POSTs.
    """
    base = f"http://localhost:{api_port}{path}"
    if not base.endswith("/"):
        base += "/"
    if session:
        base += f"?session={session}"
    return base


def jsonSnippet(api_port: int, path: str = "/mcp", session: str | None = None) -> dict[str, Any]:
    """The shape every JSON-config-based client wants under `mcpServers.taskmux`.

    `type: "http"` is required by Claude Code's MCP schema for HTTP-transport
    servers; other clients ignore it.
    """
    return {"type": "http", "url": serverUrl(api_port, path, session)}


def tomlSnippet(api_port: int, path: str = "/mcp", session: str | None = None) -> dict[str, Any]:
    """Codex CLI flavor — TOML with `[mcp_servers.taskmux]` table."""
    return {"url": serverUrl(api_port, path, session)}


def _projectRootFromCwd(start: Path) -> Path | None:
    """Internal — first ancestor of `start` containing `taskmux.toml`."""
    for candidate in [start, *start.parents]:
        if (candidate / "taskmux.toml").exists():
            return candidate
    return None


def detectProjectRootFromCwd(cwd: Path | None = None) -> Path | None:
    """Closest ancestor directory containing `taskmux.toml`, or None.

    Used by the installer to anchor `claude-project`'s `.mcp.json` at the
    project root rather than the process cwd. Without this anchor a user
    running `taskmux mcp install` from `repo/src/` would write
    `repo/src/.mcp.json` — Claude Code only loads `.mcp.json` from the
    repo root, so the install would silently fail to take effect.
    """
    return _projectRootFromCwd((cwd or Path.cwd()).resolve())


def detectSessionFromCwd(cwd: Path | None = None) -> str | None:
    """Walk upward from `cwd` looking for `taskmux.toml`; return the
    canonical daemon session key (`project_id`).

    `project_id` equals `name` for primary worktrees and `name-{worktree_id}`
    for linked worktrees — this is the key the daemon uses in its registry,
    in `recordEvent` payloads, and for proxy/tunnel routing. Returning just
    `name` here would write a URL `?session=name` that doesn't match any
    real session inside a linked worktree.

    Returns None when no `taskmux.toml` ancestor is found.
    """
    from ..config import loadProjectIdentity

    start = (cwd or Path.cwd()).resolve()
    root = _projectRootFromCwd(start)
    if root is None:
        return None
    try:
        return loadProjectIdentity(root / "taskmux.toml", cwd=start).project_id
    except Exception:  # noqa: BLE001
        # Malformed taskmux.toml — caller surfaces this; for detection
        # we just say "no session".
        return None


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------


def _atomicWrite(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _loadJson(path: Path) -> dict[str, Any]:
    """Read existing JSON config or return {} for a missing file.

    Re-raises `json.JSONDecodeError` for malformed existing files — silently
    treating an unparsable file as empty would erase the user's other
    settings on the next write. The CLI surfaces this as a per-client error
    so the user can repair the file by hand.
    """
    if not path.exists():
        return {}
    return json.loads(path.read_text() or "{}")


def _upsertJsonMcp(path: Path, name: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    config = _loadJson(path)
    servers = config.setdefault("mcpServers", {})
    servers[name] = dict(entry)
    return config


def _upsertTomlMcp(path: Path, name: str, entry: Mapping[str, Any]) -> tomlkit.TOMLDocument:
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    servers = doc.setdefault("mcp_servers", tomlkit.table())
    servers[name] = entry  # type: ignore[index]
    return doc


# ---------------------------------------------------------------------------
# Per-client paths
# ---------------------------------------------------------------------------


def _clientPath(client: str, cwd: Path | None = None) -> Path:
    home = Path.home()
    cwd = cwd or Path.cwd()
    if client == "claude":
        return home / ".claude" / "settings.json"
    if client == "claude-project":
        return cwd / ".mcp.json"
    if client == "cursor":
        return home / ".cursor" / "mcp.json"
    if client == "codex":
        return home / ".codex" / "config.toml"
    if client == "codex-project":
        return cwd / ".codex" / "config.toml"
    if client == "continue":
        return home / ".continue" / "config.json"
    raise ValueError(f"unknown client: {client!r}; expected one of {ALL_CLIENTS}")


# ---------------------------------------------------------------------------
# Public install API
# ---------------------------------------------------------------------------


def install(
    client: str,
    *,
    api_port: int,
    mcp_path: str = "/mcp",
    session: str | None = None,
    write: bool = True,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Install (or just describe) the MCP config for one client.

    `mcp_path` must match the daemon's `[mcp].path` so clients connect to
    the same URL the daemon serves. `session`, when provided, appends
    `?session=<name>` to the URL so the daemon pins this connection to
    that project (recommended default — see CLI's strict mode).
    `write=False` is "dry run" — returns the snippet that *would* be
    written, no disk mutation.
    """
    target = _clientPath(client, cwd=cwd)
    if client in ("codex", "codex-project"):
        entry = tomlSnippet(api_port, mcp_path, session)
        doc = _upsertTomlMcp(target, "taskmux", entry)
        rendered = tomlkit.dumps(doc)
        if write:
            _atomicWrite(target, rendered)
        return {
            "client": client,
            "path": str(target),
            "wrote": write,
            "format": "toml",
            "rendered": rendered,
            "snippet": dict(entry),
            "session": session,
        }

    entry = jsonSnippet(api_port, mcp_path, session)
    config = _upsertJsonMcp(target, "taskmux", entry)
    rendered = json.dumps(config, indent=2) + "\n"
    if write:
        _atomicWrite(target, rendered)
    return {
        "client": client,
        "path": str(target),
        "wrote": write,
        "format": "json",
        "rendered": rendered,
        "snippet": dict(entry),
        "session": session,
    }


def installAll(
    *,
    api_port: int,
    mcp_path: str = "/mcp",
    session: str | None = None,
    write: bool = True,
    cwd: Path | None = None,
    clients: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Install for every supported client (or a subset via `clients`).

    Per-client failures are collected so one missing parent dir doesn't
    sink the rest. Pass `clients=("claude-project", "codex-project")` for
    "install for the project-scoped agents only".
    """
    targets = tuple(clients) if clients is not None else ALL_CLIENTS
    results: list[dict[str, Any]] = []
    for client in targets:
        try:
            results.append(
                install(
                    client,
                    api_port=api_port,
                    mcp_path=mcp_path,
                    session=session,
                    write=write,
                    cwd=cwd,
                )
            )
        except Exception as e:  # noqa: BLE001
            results.append({"client": client, "error": str(e)})
    return results
