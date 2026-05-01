"""Host-wide taskmux configuration at ~/.taskmux/config.toml.

Optional file. Every key has a default. Daemon reads it on startup.

Example ~/.taskmux/config.toml:

    health_check_interval = 30
    api_port = 8765

    [tunnel.cloudflare]
    account_id = "abcd..."
    zone_id    = "ef56..."
    api_token  = "cf-pat-..."     # OR api_token_env = "CLOUDFLARE_API_TOKEN"

Schema is intentionally small — extend as new global knobs become useful.
"""

from __future__ import annotations

import os
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


class CloudflareGlobalConfig(BaseModel):
    """Cloudflare-Tunnel defaults shared by every project on this host.

    Same shape as the per-project `[tunnel.cloudflare]` block — project values
    override these one field at a time. Token never lives in project (git-tracked)
    config; embed it here OR point at an env var.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    account_id: str | None = Field(
        default=None,
        description="Cloudflare account UUID. Required for any project that tunnels.",
    )
    zone_id: str | None = Field(
        default=None,
        description=(
            "Default DNS zone for `public_hostname`. Per-project [tunnel.cloudflare] "
            "or auto-resolution from the public_hostname's apex can override."
        ),
    )
    tunnel_name: str | None = Field(
        default=None,
        description=(
            "Default cfd_tunnel name. When unset, each project gets `taskmux-{project_id}`."
        ),
    )
    api_token: str | None = Field(
        default=None,
        description=(
            "Cloudflare API token, embedded. Requires ~/.taskmux/config.toml "
            "to be mode 0600 — daemon refuses to read it otherwise. Prefer this "
            "over api_token_env when running daemon under sudo (no `-E` needed)."
        ),
    )
    api_token_env: str = Field(
        default="CLOUDFLARE_API_TOKEN",
        description=(
            "Fallback when api_token is unset: name of the env var holding the token. "
            "Token needs scopes `Account.Cloudflare Tunnel: Edit` and `Zone.DNS: Edit`."
        ),
    )


class TunnelGlobalConfig(BaseModel):
    """Container for per-backend defaults. One sub-block per supported provider."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    cloudflare: CloudflareGlobalConfig = Field(default_factory=lambda: CloudflareGlobalConfig())


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
    auto_inject_agents: bool = Field(
        default=True,
        description=(
            "Re-patch the marked taskmux block in CLAUDE.md / AGENTS.md after "
            "`taskmux add` / `taskmux remove` so agent context stays in sync "
            "with the live task list. Per-project taskmux.toml can override "
            "this with its own `auto_inject_agents = false`."
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

    tunnel: TunnelGlobalConfig = Field(
        default_factory=lambda: TunnelGlobalConfig(),
        description="Default tunnel-provider settings. Project [tunnel.*] blocks override.",
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

    # Backwards compat: pre-cascade flat keys (cloudflare_account_id /
    # cloudflare_api_token_env) are folded into the nested [tunnel.cloudflare]
    # block if present and not already set there.
    if "cloudflare_account_id" in raw or "cloudflare_api_token_env" in raw:
        legacy_account = raw.pop("cloudflare_account_id", None)
        legacy_token_env = raw.pop("cloudflare_api_token_env", None)
        tunnel_block = dict(raw.get("tunnel", {}) or {})
        cf_block = dict(tunnel_block.get("cloudflare", {}) or {})
        if legacy_account and not cf_block.get("account_id"):
            cf_block["account_id"] = legacy_account
        if legacy_token_env and not cf_block.get("api_token_env"):
            cf_block["api_token_env"] = legacy_token_env
        tunnel_block["cloudflare"] = cf_block
        raw["tunnel"] = tunnel_block

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


def hasEmbeddedToken(config: GlobalConfig) -> bool:
    """Whether the in-memory config carries a Cloudflare API token directly."""
    return bool(config.tunnel.cloudflare.api_token)


def globalConfigModeOk(path: Path | None = None) -> tuple[bool, int | None]:
    """Return (ok, mode). True when reading the file is safe.

    Reading is safe when either:
      - the file doesn't exist, or
      - the file does NOT embed a token (no secret to leak), or
      - the file is mode 0600 or stricter.

    Used as a safety rail by the daemon before reading an embedded token, and
    by `taskmux tunnel config` for display.
    """
    p = path or globalConfigPath()
    if not p.exists():
        return True, None
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        return True, None
    try:
        raw = tomllib.loads(p.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        # If we can't parse it, defer to mode check — strict path.
        return mode == 0o600, mode
    has_token = bool(((raw.get("tunnel") or {}).get("cloudflare") or {}).get("api_token"))
    if not has_token:
        return True, mode
    # Mask: only the user bits should be set; group/other read or write fails.
    return (mode & 0o077) == 0, mode


def _scrubNones(d: dict) -> dict:
    """Recursively drop keys whose value is None — tomlkit can't represent them."""
    out = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            scrubbed = _scrubNones(v)
            out[k] = scrubbed
        else:
            out[k] = v
    return out


def _writeTunnelTable(tunnel: TunnelGlobalConfig):  # type: ignore[no-untyped-def]
    cf = tunnel.cloudflare
    cf_dump = _scrubNones(cf.model_dump())
    # Suppress the api_token_env default — only emit when overridden.
    if cf_dump.get("api_token_env") == "CLOUDFLARE_API_TOKEN" and "api_token" not in cf_dump:
        cf_dump.pop("api_token_env", None)
    if not cf_dump:
        return None
    outer = tomlkit.table()
    inner = tomlkit.table()
    for k, v in cf_dump.items():
        inner.add(k, v)
    outer.add("cloudflare", inner)
    return outer


def writeGlobalConfig(config: GlobalConfig, path: Path | None = None) -> Path:
    """Write the config back to ~/.taskmux/config.toml.

    When the cloudflare api_token is embedded, the file is chmodded 0600 — the
    daemon refuses to read it otherwise. Always write atomically via temp file.
    """
    p = path or globalConfigPath()
    ensureTaskmuxDir()
    doc = tomlkit.document()
    flat = config.model_dump()
    flat.pop("tunnel", None)
    for field, value in flat.items():
        if value is None:
            continue
        doc.add(field, value)
    tun_tbl = _writeTunnelTable(config.tunnel)
    if tun_tbl is not None:
        doc.add(tomlkit.nl())
        doc.add("tunnel", tun_tbl)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    os.replace(tmp, p)
    if hasEmbeddedToken(config):
        with _suppress_oserror():
            os.chmod(p, 0o600)
    return p


def updateGlobalConfig(updates: dict[str, Any]) -> GlobalConfig:
    """Read, merge updates (dotted-path or top-level), validate, write.

    `updates` may use dotted paths like {"tunnel.cloudflare.zone_id": "..."} or
    nested dicts {"tunnel": {"cloudflare": {"zone_id": "..."}}}. Both are merged.
    """
    current = loadGlobalConfig().model_dump()
    for key, value in updates.items():
        if "." in key:
            target = current
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        elif isinstance(value, dict) and isinstance(current.get(key), dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    try:
        new = GlobalConfig(**current)
    except ValidationError as e:
        raise TaskmuxError(ErrorCode.CONFIG_VALIDATION, detail=str(e)) from e
    writeGlobalConfig(new)
    return new


def _suppress_oserror():
    import contextlib as _c

    return _c.suppress(OSError)
