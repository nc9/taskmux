"""Config lint — environment checks beyond Pydantic's structural validation.

Pydantic (models.py) rejects configs that can never be valid: bad task names,
duplicate hosts, unknown depends_on, cycles. This module checks things that
depend on the machine the config runs on — a missing cwd, an executable not
on PATH — which must stay non-fatal: the directory may appear after a clone
or build, so a config that lints dirty still loads.

Surfaced via `taskmux check` and as daemon-log warnings on project (re)load.
"""

from __future__ import annotations

import re
import shlex
import shutil
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .models import TaskmuxConfig

# A command containing any of these runs through real shell parsing (pipes,
# substitution, compound statements) — too ambiguous for a which() heuristic.
# Quotes are NOT meta: shlex handles them, and quoted args are common
# (`server --name "my app"`); malformed quoting hits shlex's ValueError path.
_SHELL_META = set("|&;<>()$`\\*?[]{}~\n")

# Builtins `/bin/sh -c` resolves without consulting PATH.
_SH_BUILTINS = frozenset(
    {".", ":", "cd", "eval", "exec", "exit", "set", "source", "test", "true", "false", "wait"}
)

_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning"
    code: str
    task: str | None
    message: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "task": self.task,
            "message": self.message,
        }


def resolveCwd(cwd: str | None, config_dir: Path | None) -> Path | None:
    """Mirror Supervisor._resolve_cwd: expanduser, relative paths anchor at
    the config file's directory."""
    if not cwd:
        return None
    p = Path(cwd).expanduser()
    if p.is_absolute():
        return p
    if config_dir is not None:
        return (config_dir / p).resolve()
    return p


def _command_head(command: str) -> str | None:
    """First executable token of a simple command, or None when the command
    uses shell features that make static resolution unreliable."""
    if any(c in _SHELL_META for c in command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for tok in tokens:
        if _ENV_PREFIX.match(tok):
            continue  # leading VAR=value assignments
        return tok
    return None


def _check_command(task: str, command: str, cwd: Path | None) -> Issue | None:
    head = _command_head(command)
    if head is None or head in _SH_BUILTINS:
        return None
    if "/" in head:
        p = Path(head).expanduser()
        if not p.is_absolute() and cwd is not None:
            p = cwd / p
        if not p.exists():
            return Issue(
                "warning",
                "command_not_found",
                task,
                f"command executable not found: {p} (PATH may differ under the daemon)",
            )
        return None
    if shutil.which(head) is None:
        return Issue(
            "warning",
            "command_not_found",
            task,
            f"command executable {head!r} not found on PATH (PATH may differ under the daemon)",
        )
    return None


def validateEnvironment(config: TaskmuxConfig, config_dir: Path | None) -> list[Issue]:
    """Lint a parsed config against the local machine. Returns issues sorted
    errors-first; never raises."""
    issues: list[Issue] = []
    for name, task in config.tasks.items():
        cwd = resolveCwd(task.cwd, config_dir)
        if cwd is not None and not cwd.is_dir():
            issues.append(
                Issue(
                    "error",
                    "cwd_missing",
                    name,
                    f"cwd does not exist: {cwd} — the task can never start "
                    "(and will churn the auto-restart loop if it has a restart policy)",
                )
            )
            cwd = None  # don't resolve relative executables against it

        cmd_issue = _check_command(name, task.command, cwd)
        if cmd_issue is not None:
            issues.append(cmd_issue)

        if task.health_url is not None:
            parsed = urllib.parse.urlsplit(task.health_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                issues.append(
                    Issue(
                        "warning",
                        "health_url_invalid",
                        name,
                        f"health_url {task.health_url!r} is not a valid http(s) URL — "
                        "the probe will always fail and trigger restarts",
                    )
                )

        for dep in task.depends_on:
            dep_cfg = config.tasks.get(dep)
            if dep_cfg is not None and task.auto_start and not dep_cfg.auto_start:
                issues.append(
                    Issue(
                        "warning",
                        "dep_not_auto_started",
                        name,
                        f"depends on {dep!r} which has auto_start = false — "
                        "the dependency won't be up when this task auto-starts",
                    )
                )

    issues.sort(key=lambda i: (i.severity != "error", i.task or "", i.code))
    return issues
