"""Pydantic models for Taskmux configuration."""

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .errors import ErrorCode, TaskmuxError

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

_DNS_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Per-label DNS validation for FQDNs (used for `public_hostname`). Each label
# is 1–63 chars of [a-z0-9-], not starting or ending with a hyphen. Underscores
# are valid in some records (e.g. SRV) but not for HTTPS names.
_DNS_LABEL = re.compile(r"^(?=.{1,63}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


class TunnelKind(StrEnum):
    """Per-task public-tunnel backend."""

    CLOUDFLARE = "cloudflare"
    NOOP = "noop"


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
    tunnel: TunnelKind | None = None
    public_hostname: str | None = None
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
        """Accept None, a DNS slug, `@` (apex, normalised to ""), or `*` (wildcard).

        Apex is stored as the empty string internally so every downstream
        lookup (`(project, host)` route key, FQDN composition, URL display)
        stays trivially consistent — there's only one representation.
        """
        if v is None:
            return v
        if v == "@":
            return ""
        if v == "*":
            return v
        if not _DNS_SLUG.match(v):
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    f"Invalid host {v!r}. Must be one of: a DNS-safe slug "
                    "(lowercase letters, digits, hyphens; not at start/end, "
                    "e.g. 'api', 'web-1'), '@' for apex, or '*' for wildcard."
                ),
            )
        return v

    @field_validator("public_hostname")
    @classmethod
    def _validate_public_hostname(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip().lower().rstrip(".")
        if not v or "." not in v:
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=f"Invalid public_hostname {v!r}: must be a fully-qualified domain name.",
            )
        for label in v.split("."):
            if not _DNS_LABEL.match(label):
                raise TaskmuxError(
                    ErrorCode.CONFIG_VALIDATION,
                    detail=(
                        f"Invalid public_hostname {v!r}: label {label!r} must be 1–63 "
                        "lowercase letters/digits/hyphens, not starting or ending with a hyphen."
                    ),
                )
        return v

    @model_validator(mode="after")
    def _validate_tunnel_requires_host(self) -> "TaskConfig":
        if self.tunnel is None:
            return self
        if self.host is None:
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    "Task with `tunnel` set must also set `host` — taskmux "
                    "tunnels public traffic through the proxy, which routes by host."
                ),
            )
        if self.host == "*":
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    "Wildcard host (`*`) cannot be tunneled: there's no single FQDN "
                    "to point a public hostname at. Use a specific host or `@` (apex)."
                ),
            )
        if self.tunnel == TunnelKind.CLOUDFLARE and not self.public_hostname:
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail='`tunnel = "cloudflare"` requires `public_hostname` to be set.',
            )
        return self

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


class CloudflareTunnelProjectConfig(_StrictConfig):
    """Per-project Cloudflare Tunnel settings.

    Credentials (account_id, api_token) live in the host-wide global config so
    one set of secrets covers every project. Per-project knobs: zone selection
    and the tunnel name to create / reuse.
    """

    zone_id: str | None = None
    """Cloudflare zone ID for `public_hostname` DNS routing. Required when any
    task in this project sets `tunnel = "cloudflare"`."""

    tunnel_name: str | None = None
    """Name for the cfd_tunnel created against the account. Defaults to the
    project_id at sync time (so worktrees get their own tunnels)."""


class TunnelProjectConfig(_StrictConfig):
    """Per-project tunnel block. One sub-block per supported backend."""

    cloudflare: CloudflareTunnelProjectConfig = CloudflareTunnelProjectConfig()


class TaskmuxConfig(_StrictConfig):
    """Top-level taskmux.toml schema."""

    name: str = "taskmux"
    auto_start: bool = True
    auto_daemon: bool = False
    auto_inject_agents: bool | None = None
    """Per-project override for the global `auto_inject_agents` knob.
    None = inherit from ~/.taskmux/config.toml; True/False forces it."""
    hooks: HookConfig = HookConfig()
    worktree: WorktreeConfig = WorktreeConfig()
    tunnel: TunnelProjectConfig = TunnelProjectConfig()
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
        """Reject duplicate host entries.

        A project may have at most one apex (`""`, written as `@`), at most
        one wildcard (`"*"`), and unique specific subdomains.
        """
        seen: dict[str, str] = {}
        for name, cfg in self.tasks.items():
            if cfg.host is None:
                continue
            if cfg.host in seen:
                if cfg.host == "":
                    detail = f"Duplicate apex host (`@`) on tasks {seen[cfg.host]!r} and {name!r}"
                elif cfg.host == "*":
                    detail = (
                        f"Duplicate wildcard host (`*`) on tasks {seen[cfg.host]!r} and {name!r}"
                    )
                else:
                    detail = f"Duplicate host {cfg.host!r} on tasks {seen[cfg.host]!r} and {name!r}"
                raise TaskmuxError(ErrorCode.CONFIG_VALIDATION, detail=detail)
            seen[cfg.host] = name
        return self

    @model_validator(mode="after")
    def _validate_tunnel_credentials(self) -> "TaskmuxConfig":
        """If any task uses `tunnel = "cloudflare"`, the project must declare
        a Cloudflare zone the public hostname will land in. The actual API
        token + account ID live in the host-wide global config."""
        uses_cf = any(t.tunnel == TunnelKind.CLOUDFLARE for t in self.tasks.values())
        if uses_cf and not self.tunnel.cloudflare.zone_id:
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    'A task uses `tunnel = "cloudflare"` but [tunnel.cloudflare] '
                    "has no `zone_id`. Add the Cloudflare zone ID for the "
                    "domain hosting your `public_hostname`."
                ),
            )
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
