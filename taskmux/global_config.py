"""Host-wide taskmux configuration at ~/.taskmux/config.toml.

Optional file. Every key has a default. Daemon reads it on startup.

Example ~/.taskmux/config.toml:

    health_check_interval = 30
    api_port = 8765

Schema is intentionally small — extend as new global knobs become useful.
"""

from __future__ import annotations

import re
import tomllib
import warnings
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .errors import ErrorCode, TaskmuxError
from .paths import ensureTaskmuxDir, globalConfigPath

# Single DNS label: ascii letters / digits / inner hyphens, 1–63 chars.
# Deliberately strict — the value flows into /etc/resolver paths and
# PowerShell command strings.
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class GlobalConfig(BaseModel):
    """Host-wide taskmux config. Frozen — to mutate, build a new instance."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    health_check_interval: int = Field(
        default=30,
        ge=1,
        description="Seconds between daemon health-check sweeps across all projects.",
    )
    api_port: int = Field(
        default=8765,
        ge=1,
        le=65535,
        description="WebSocket API port the daemon listens on.",
    )
    proxy_enabled: bool = Field(
        default=True,
        description="Run the HTTPS reverse proxy that exposes {host}.{project}.localhost.",
    )
    proxy_https_port: int = Field(
        default=443,
        ge=1,
        le=65535,
        description="HTTPS port for the proxy. Binding <1024 needs root or setcap.",
    )
    proxy_bind: str = Field(
        default="127.0.0.1",
        description=(
            "Interface for the proxy listener. Defaults to loopback so "
            "{host}.{project}.localhost stays on this machine. Set to "
            "0.0.0.0 to expose to the LAN — only do this on trusted networks."
        ),
    )
    host_resolver: str = Field(
        default="etc_hosts",
        description=(
            "How taskmux makes proxy hostnames reachable. 'etc_hosts' writes "
            "a managed block to the system hosts file (needs root once at "
            "daemon startup, dropped immediately after). 'dns_server' runs "
            "an in-process DNS server and delegates the managed TLD to it "
            "via /etc/resolver/<tld> (macOS), systemd-resolved (Linux), or "
            "NRPT (Windows) — supports dynamic adds with no daemon restart. "
            "'noop' if you handle resolution externally."
        ),
    )
    dns_server_port: int = Field(
        default=5454,
        ge=1,
        le=65535,
        description=(
            "UDP port the in-process DNS server binds to (loopback only). "
            "Used only when host_resolver = 'dns_server'. Avoid 5353 — that's "
            "mDNS (Bonjour, Brave, Chrome cast). Set to 53 if using NRPT on Windows."
        ),
    )
    dns_managed_tld: str = Field(
        default="localhost",
        description=(
            "TLD that the DNS server claims authority over. Default matches "
            "the URL scheme {host}.{project}.localhost. Catch-all: any unmapped "
            "subdomain in this TLD resolves to 127.0.0.1. Must be a single DNS "
            "label (lowercase ASCII letters / digits / inner hyphens) — flows "
            "into /etc/resolver paths and PowerShell commands, so we reject "
            "anything that could path-traverse or shell-escape."
        ),
    )

    @field_validator("dns_managed_tld")
    @classmethod
    def _validate_dns_managed_tld(cls, v: str) -> str:
        if not _DNS_LABEL_RE.match(v):
            raise TaskmuxError(
                ErrorCode.CONFIG_VALIDATION,
                detail=(
                    f"Invalid dns_managed_tld {v!r}: must be a single DNS label "
                    "(lowercase letters, digits, hyphens; not at start/end; max 63 chars)."
                ),
            )
        return v


def loadGlobalConfig(path: Path | None = None) -> GlobalConfig:
    """Read ~/.taskmux/config.toml. Returns defaults if missing or empty.

    Unknown keys generate a warning and are dropped (extra='ignore' in model).
    Validation failures raise TaskmuxError(CONFIG_VALIDATION).
    """
    p = path or globalConfigPath()
    if not p.exists():
        return GlobalConfig()
    try:
        raw = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise TaskmuxError(ErrorCode.CONFIG_PARSE_ERROR, path=str(p), detail=str(e)) from e

    known = set(GlobalConfig.model_fields.keys())
    unknown = set(raw.keys()) - known
    if unknown:
        warnings.warn(
            f"Unknown global config keys ignored: {sorted(unknown)}",
            stacklevel=2,
        )

    try:
        return GlobalConfig(**raw)
    except ValidationError as e:
        raise TaskmuxError(ErrorCode.CONFIG_VALIDATION, detail=str(e)) from e


def writeGlobalConfig(config: GlobalConfig, path: Path | None = None) -> Path:
    """Write the config back to ~/.taskmux/config.toml (preserves formatting)."""
    p = path or globalConfigPath()
    ensureTaskmuxDir()
    doc = tomlkit.document()
    for field, value in config.model_dump().items():
        doc.add(field, value)
    p.write_text(tomlkit.dumps(doc))
    return p


def updateGlobalConfig(updates: dict[str, Any]) -> GlobalConfig:
    """Read, merge updates, validate, write. Returns the new config."""
    current = loadGlobalConfig().model_dump()
    current.update(updates)
    try:
        new = GlobalConfig(**current)
    except ValidationError as e:
        raise TaskmuxError(ErrorCode.CONFIG_VALIDATION, detail=str(e)) from e
    writeGlobalConfig(new)
    return new
