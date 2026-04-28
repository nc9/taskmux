"""Pydantic models for Taskmux configuration."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .errors import ErrorCode, TaskmuxError

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

_DNS_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def slugify(value: str) -> str:
    """Make value DNS-label-safe: lowercase, only [a-z0-9-], no leading/trailing hyphens."""
    s = re.sub(r"[^a-z0-9-]+", "-", value.lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "taskmux"


class RestartPolicy(StrEnum):
    """Docker-style restart policy for tasks."""

    NO = "no"
    ON_FAILURE = "on-failure"
    ALWAYS = "always"


class _StrictConfig(BaseModel):
    """Base config: frozen, rejects unknown keys."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_keys(cls, values: dict) -> dict:
        if not isinstance(values, dict):
            return values
        known = set(cls.model_fields.keys())
        unknown = sorted(set(values.keys()) - known)
        if unknown:
            raise TaskmuxError(
                ErrorCode.CONFIG_UNKNOWN_KEYS,
                keys=", ".join(repr(k) for k in unknown),
            )
        return values


class HookConfig(_StrictConfig):
    """Lifecycle hooks for tasks or global config."""

    before_start: str | None = None
    after_start: str | None = None
    before_stop: str | None = None
    after_stop: str | None = None


class TaskConfig(_StrictConfig):
    """Single task definition."""

    command: str
    auto_start: bool = True
    cwd: str | None = None
    host: str | None = None
    host_path: str = "/"
    health_check: str | None = None
    health_url: str | None = None
    health_expected_status: int = 200
    health_expected_body: str | None = None
    health_interval: int = 10
    health_timeout: int = 5
    health_retries: int = 3
    stop_grace_period: int = 5
    max_restarts: int = 5
    restart_backoff: float = 2.0
    restart_policy: RestartPolicy = RestartPolicy.ON_FAILURE
    log_file: str | None = None
    log_max_size: str = "10MB"
    log_max_files: int = 3
    depends_on: list[str] = []
    hooks: HookConfig = HookConfig()

    @field_validator("host")
    @classmethod
    def _validate_host(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _DNS_SLUG.match(v):
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    f"Invalid host {v!r}. Must be DNS-safe: lowercase letters, digits, "
                    "and hyphens (not at start/end). E.g. 'api', 'web-1'."
                ),
            )
        return v

    @field_validator("log_max_size")
    @classmethod
    def _validate_log_max_size(cls, v: str) -> str:
        upper = v.strip().upper()
        for suffix in sorted(_SIZE_UNITS, key=len, reverse=True):
            if upper.endswith(suffix):
                num = upper[: -len(suffix)].strip()
                if num and re.match(r"^\d+(\.\d+)?$", num):
                    return v
                break
        if re.match(r"^\d+$", upper):
            return v
        raise TaskmuxError(
            ErrorCode.CONFIG_VALIDATION,
            detail=f"Invalid size format: {v!r}. Use e.g. '10MB', '500KB', '1GB'",
        )


class WorktreeConfig(_StrictConfig):
    """Git worktree behaviour. When taskmux runs inside a linked worktree it
    derives a worktree_id and composes `project_id = name-{worktree_id}` so
    each worktree has its own session, state, ports, and URL namespace."""

    enabled: bool = True
    separator: str = "-"
    main_branches: list[str] = ["main", "master"]


class TaskmuxConfig(_StrictConfig):
    """Top-level taskmux.toml schema."""

    name: str = "taskmux"
    auto_start: bool = True
    auto_daemon: bool = False
    hooks: HookConfig = HookConfig()
    worktree: WorktreeConfig = WorktreeConfig()
    tasks: dict[str, TaskConfig] = {}

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _DNS_SLUG.match(v):
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    f"Invalid project name {v!r}. Must be DNS-safe (used in URLs as "
                    "{host}.{name}.localhost): lowercase letters, digits, and hyphens "
                    "(not at start/end)."
                ),
            )
        return v

    @model_validator(mode="after")
    def _validate_unique_hosts(self) -> "TaskmuxConfig":
        seen: dict[str, str] = {}
        for name, cfg in self.tasks.items():
            if cfg.host is None:
                continue
            if cfg.host in seen:
                raise TaskmuxError(
                    ErrorCode.CONFIG_VALIDATION,
                    detail=(
                        f"Duplicate host {cfg.host!r} on tasks {seen[cfg.host]!r} and {name!r}"
                    ),
                )
            seen[cfg.host] = name
        return self

    @model_validator(mode="after")
    def _validate_depends_on(self) -> "TaskmuxConfig":
        """Reject unknown depends_on references and cycles."""
        task_names = set(self.tasks.keys())
        for name, cfg in self.tasks.items():
            for dep in cfg.depends_on:
                if dep not in task_names:
                    raise TaskmuxError(ErrorCode.TASK_DEPENDENCY_MISSING, task=name, dep=dep)
                if dep == name:
                    raise TaskmuxError(ErrorCode.TASK_DEPENDENCY_SELF, task=name)

        # Cycle detection via DFS
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in task_names}

        def dfs(node: str) -> None:
            color[node] = GRAY
            for dep in self.tasks[node].depends_on:
                if color[dep] == GRAY:
                    raise TaskmuxError(ErrorCode.TASK_DEPENDENCY_CYCLE, dep=dep)
                if color[dep] == WHITE:
                    dfs(dep)
            color[node] = BLACK

        for n in task_names:
            if color[n] == WHITE:
                dfs(n)

        return self
