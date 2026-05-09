"""Render worktree-scoped identity + task URLs as shell-evalable env exports.

Used by `taskmux env`. Pure rendering — no IPC, no filesystem. Caller passes
in the resolved identity + task list; this module formats it for a target
shell.

Why a separate module: keeps `cli.py` thin and lets us unit-test the
format precisely (quoting, prefix substitution, task-name normalisation,
shell dialects) without spinning up a TaskmuxCLI.
"""

from __future__ import annotations

import json
import re
import shlex

from .url import taskUrl

DEFAULT_PREFIX = "TASKMUX_"
SUPPORTED_SHELLS: tuple[str, ...] = ("zsh", "bash", "fish", "posix")

_NON_VAR_CHAR = re.compile(r"[^A-Z0-9_]+")


def normalizeTaskVar(name: str) -> str:
    """Task name → suffix safe for an env var. `web-1` → `WEB_1`."""
    upper = name.upper().replace("-", "_")
    cleaned = _NON_VAR_CHAR.sub("_", upper).strip("_")
    return cleaned or "TASK"


def _fishQuote(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _exportLine(name: str, value: str, shell: str) -> str:
    if shell == "fish":
        return f"set -gx {name} {_fishQuote(value)}"
    return f"export {name}={shlex.quote(value)}"


def _baseHost(projectId: str) -> str:
    return f"{projectId}.localhost"


def renderEnv(
    *,
    project: str,
    project_id: str,
    branch: str | None,
    worktree: str | None,
    is_linked: bool,
    tasks: list[tuple[str, str]],
    shell: str = "posix",
    prefix: str = DEFAULT_PREFIX,
    include_urls: bool = True,
) -> str:
    """Build the full shell-export block.

    `tasks` is a list of (task_name, host) — host as stored in TaskConfig.
    Wildcard hosts (`"*"`) are skipped. Apex hosts (`""`) collapse to base.
    """
    if shell not in SUPPORTED_SHELLS:
        raise ValueError(f"unsupported shell {shell!r}")

    pairs: list[tuple[str, str]] = [
        (f"{prefix}PROJECT", project),
        (f"{prefix}PROJECT_ID", project_id),
        (f"{prefix}BASE_HOST", _baseHost(project_id)),
        (f"{prefix}IS_LINKED", "1" if is_linked else "0"),
    ]
    if branch is not None:
        pairs.append((f"{prefix}BRANCH", branch))
    if worktree is not None:
        pairs.append((f"{prefix}WORKTREE", worktree))

    if include_urls:
        for name, host in tasks:
            if host == "*":
                continue
            pairs.append((f"{prefix}URL_{normalizeTaskVar(name)}", taskUrl(project_id, host)))

    lines = [_exportLine(k, v, shell) for k, v in pairs]
    return "\n".join(lines) + "\n"


def renderEnvJson(
    *,
    project: str,
    project_id: str,
    branch: str | None,
    worktree: str | None,
    is_linked: bool,
    tasks: list[tuple[str, str]],
    prefix: str = DEFAULT_PREFIX,
    include_urls: bool = True,
) -> str:
    """JSON form of the same data — for non-shell consumers."""
    payload: dict[str, str | dict[str, str]] = {
        f"{prefix}PROJECT": project,
        f"{prefix}PROJECT_ID": project_id,
        f"{prefix}BASE_HOST": _baseHost(project_id),
        f"{prefix}IS_LINKED": "1" if is_linked else "0",
    }
    if branch is not None:
        payload[f"{prefix}BRANCH"] = branch
    if worktree is not None:
        payload[f"{prefix}WORKTREE"] = worktree
    if include_urls:
        for name, host in tasks:
            if host == "*":
                continue
            payload[f"{prefix}URL_{normalizeTaskVar(name)}"] = taskUrl(project_id, host)
    return json.dumps(payload, indent=2) + "\n"
