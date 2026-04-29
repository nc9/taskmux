"""Tunnel orchestration: shared between CLI prompts, daemon WS API, and tests.

The wizard is the single source of truth for "set up a Cloudflare tunnel from
zero". It does NOT prompt — that's the CLI's job. It exposes:

  - ``preflight()`` — every safety check, returning structured results.
  - ``enable()``    — idempotent setup: create-or-load tunnel, write configs,
                      ensure DNS routes, return URLs.
  - ``disable()``   — stop forwarding, optionally prune config blocks.
  - ``test()``      — preflight + reachability probes, no mutations.

Every entry point returns plain dataclasses so the CLI can render to text or
``--json`` without coupling to either.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from .config import loadProjectIdentity, writeConfig
from .errors import ErrorCode, TaskmuxError
from .global_config import (
    GlobalConfig,
    globalConfigModeOk,
    loadGlobalConfig,
    updateGlobalConfig,
)
from .models import (
    CloudflareTunnelProjectConfig,
    TaskConfig,
    TaskmuxConfig,
    TunnelKind,
    TunnelProjectConfig,
)
from .paths import globalConfigPath
from .tunnels import EffectiveCloudflareConfig, resolveCloudflareConfig

_CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
_CLOUDFLARED_INSTALL = (
    "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One row in a preflight report."""

    name: str
    ok: bool
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class EnableResult:
    ok: bool
    backend: str
    project_id: str
    tunnel_name: str | None
    tunnel_id: str | None
    public_urls: dict[str, str]  # task_name → public URL
    config: dict
    preflight: PreflightReport
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "project_id": self.project_id,
            "tunnel_name": self.tunnel_name,
            "tunnel_id": self.tunnel_id,
            "public_urls": self.public_urls,
            "config": self.config,
            "preflight": self.preflight.to_dict(),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Token / API helpers
# ---------------------------------------------------------------------------


async def _api(
    session: aiohttp.ClientSession, method: str, path: str, **kwargs: Any
) -> tuple[bool, Any, list[str]]:
    """Hit Cloudflare's API. Returns (success, result, errors)."""
    url = f"{_CLOUDFLARE_API}{path}"
    async with session.request(method, url, **kwargs) as resp:
        text = await resp.text()
        try:
            payload = await resp.json() if text else {}
        except Exception:  # noqa: BLE001
            payload = {}
        if not isinstance(payload, dict):
            return False, None, [f"non-dict response: {text[:200]}"]
        success = bool(payload.get("success"))
        result = payload.get("result")
        errors = [str(e.get("message", "?")) for e in payload.get("errors", []) or []]
        if resp.status >= 400 and not errors:
            errors.append(f"HTTP {resp.status}")
        return success, result, errors


async def _verify_token(token: str) -> tuple[bool, str | None]:
    """Cloudflare's token-verify endpoint. Returns (active, detail_or_error)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        ok, result, errors = await _api(session, "GET", "/user/tokens/verify")
        if not ok:
            return False, "; ".join(errors) or "token rejected"
        if isinstance(result, dict) and result.get("status") != "active":
            return False, f"token status: {result.get('status')}"
        return True, None


async def _list_zones(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        ok, result, errors = await _api(session, "GET", "/zones", params={"per_page": 50})
        if not ok or not isinstance(result, list):
            raise TaskmuxError(
                ErrorCode.INTERNAL,
                detail=f"Failed to list Cloudflare zones: {'; '.join(errors)}",
            )
        return result


async def _resolve_zone_for_hostname(token: str, hostname: str) -> dict | None:
    """Walk the FQDN's apex candidates and find the longest zone match."""
    zones = await _list_zones(token)
    by_name = {z["name"]: z for z in zones}
    parts = hostname.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in by_name:
            return by_name[candidate]
    return None


async def _list_dns_records(token: str, zone_id: str, name: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        ok, result, _ = await _api(
            session,
            "GET",
            f"/zones/{zone_id}/dns_records",
            params={"name": name},
        )
        return result if ok and isinstance(result, list) else []


async def _ensure_tunnel(token: str, account_id: str, tunnel_name: str) -> tuple[str, str]:
    """Get or create a cfd_tunnel, returning (id, token)."""
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        ok, result, errors = await _api(
            session,
            "GET",
            f"/accounts/{account_id}/cfd_tunnel",
            params={"name": tunnel_name, "is_deleted": "false"},
        )
        if not ok:
            raise TaskmuxError(
                ErrorCode.INTERNAL,
                detail=f"Cloudflare tunnel lookup failed: {'; '.join(errors)}",
            )
        if isinstance(result, list) and result:
            tid = result[0]["id"]
        else:
            ok, created, errors = await _api(
                session,
                "POST",
                f"/accounts/{account_id}/cfd_tunnel",
                json={"name": tunnel_name, "config_src": "cloudflare"},
            )
            if not ok or not isinstance(created, dict):
                raise TaskmuxError(
                    ErrorCode.INTERNAL,
                    detail=f"Cloudflare tunnel create failed: {'; '.join(errors)}",
                )
            tid = created["id"]

        ok, token_result, errors = await _api(
            session, "GET", f"/accounts/{account_id}/cfd_tunnel/{tid}/token"
        )
        if not ok:
            raise TaskmuxError(
                ErrorCode.INTERNAL,
                detail=f"Tunnel token fetch failed: {'; '.join(errors)}",
            )
        return tid, str(token_result)


async def _route_dns(
    token: str, account_id: str, tunnel_id: str, hostname: str
) -> tuple[bool, str]:
    """POST a DNS route. Returns (ok, detail). Treats 'already exists' as ok."""
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        ok, _result, errors = await _api(
            session,
            "POST",
            f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/routes/dns",
            json={"hostname": hostname},
        )
        if ok:
            return True, "created"
        joined = " ".join(errors).lower()
        if "already exists" in joined or "duplicate" in joined or "1003" in joined:
            return True, "already_exists"
        return False, "; ".join(errors)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _project_uses_cloudflare(cfg: TaskmuxConfig) -> list[tuple[str, TaskConfig]]:
    return [(name, t) for name, t in cfg.tasks.items() if t.tunnel == TunnelKind.CLOUDFLARE]


def _check_file_mode() -> CheckResult:
    ok, mode = globalConfigModeOk()
    if ok:
        return CheckResult(
            "config_file_mode",
            True,
            f"~/.taskmux/config.toml mode {oct(mode) if mode is not None else '(missing)'}",
        )
    return CheckResult(
        "config_file_mode",
        False,
        f"~/.taskmux/config.toml is mode {oct(mode) if mode else '?'} but contains api_token",
        fix="chmod 600 ~/.taskmux/config.toml",
    )


def _check_cloudflared_present() -> CheckResult:
    if shutil.which("cloudflared"):
        return CheckResult("cloudflared_binary", True, "cloudflared in PATH")
    install_hint = "brew install cloudflared" if shutil.which("brew") else _CLOUDFLARED_INSTALL
    return CheckResult(
        "cloudflared_binary",
        False,
        "cloudflared not found in PATH",
        fix=install_hint,
    )


async def _check_token(token: str | None) -> CheckResult:
    if not token:
        return CheckResult(
            "api_token",
            False,
            "Cloudflare API token not configured",
            fix="taskmux tunnel config set --global api_token=<token>",
        )
    ok, detail = await _verify_token(token)
    if ok:
        return CheckResult("api_token", True, "token verified by Cloudflare")
    return CheckResult(
        "api_token", False, f"token rejected: {detail}", fix="rotate the token in dash"
    )


async def _check_account_and_zone(
    token: str | None, eff: EffectiveCloudflareConfig, hostnames: list[str]
) -> list[CheckResult]:
    out: list[CheckResult] = []
    if not token:
        return out
    if not eff.account_id:
        out.append(
            CheckResult(
                "account_id",
                False,
                "Cloudflare account_id is not set",
                fix="taskmux tunnel config set --global account_id=<id>",
            )
        )
    else:
        out.append(CheckResult("account_id", True, eff.account_id))

    # Resolve / verify zone — single zone list request reused for every hostname.
    try:
        zones = await _list_zones(token)
    except TaskmuxError as e:
        out.append(CheckResult("zones_list", False, str(e), fix="check token DNS:Edit scope"))
        return out

    by_id = {z["id"]: z for z in zones}
    by_name = {z["name"]: z for z in zones}

    if eff.zone_id and eff.zone_id not in by_id:
        out.append(
            CheckResult(
                "zone",
                False,
                f"Configured zone_id {eff.zone_id!r} is not visible to this token",
                fix="grant Zone:DNS:Edit on that zone, or pick another with `tunnel config set`",
            )
        )

    for fqdn in hostnames:
        zone = None
        for i in range(len(fqdn.split(".")) - 1):
            candidate = ".".join(fqdn.split(".")[i:])
            if candidate in by_name:
                zone = by_name[candidate]
                break
        if zone is None:
            out.append(
                CheckResult(
                    f"hostname:{fqdn}",
                    False,
                    f"No zone matching {fqdn!r} is editable by this token",
                    fix="add the zone to your Cloudflare account, or change public_hostname",
                )
            )
        else:
            out.append(CheckResult(f"hostname:{fqdn}", True, f"matches zone {zone['name']}"))
    return out


async def _check_dns_collisions(
    token: str | None,
    zones_by_fqdn: dict[str, dict],
    hostnames: list[str],
    tunnel_id: str | None,
) -> list[CheckResult]:
    """Refuse to overwrite a non-tunnel CNAME / A record at the public hostname."""
    out: list[CheckResult] = []
    if not token:
        return out
    for fqdn in hostnames:
        zone = zones_by_fqdn.get(fqdn)
        if zone is None:
            continue
        records = await _list_dns_records(token, zone["id"], fqdn)
        if not records:
            out.append(CheckResult(f"dns:{fqdn}", True, "no existing record"))
            continue
        # Single record pointing at our tunnel is the happy path.
        for rec in records:
            content = str(rec.get("content", ""))
            is_taskmux_tunnel = tunnel_id is not None and content == f"{tunnel_id}.cfargotunnel.com"
            if is_taskmux_tunnel:
                out.append(
                    CheckResult(
                        f"dns:{fqdn}",
                        True,
                        f"existing CNAME points to this tunnel ({tunnel_id})",
                    )
                )
                break
            if rec.get("type") == "CNAME" and content.endswith(".cfargotunnel.com"):
                out.append(
                    CheckResult(
                        f"dns:{fqdn}",
                        False,
                        f"existing CNAME points to a different tunnel: {content}",
                        fix="delete the record in dash, or change public_hostname",
                    )
                )
                break
            out.append(
                CheckResult(
                    f"dns:{fqdn}",
                    False,
                    f"existing {rec.get('type')} record: {content}",
                    fix="delete the record in dash, or change public_hostname",
                )
            )
            break
    return out


async def preflight(
    *,
    project_id: str,
    project_cfg: TaskmuxConfig,
    global_cfg: GlobalConfig,
    api_token_override: str | None = None,
    tunnel_id: str | None = None,
) -> PreflightReport:
    """Run every check and return the report. Cheap when offline (skips API)."""
    report = PreflightReport()
    cf_tasks = _project_uses_cloudflare(project_cfg)
    hostnames = [t.public_hostname for _, t in cf_tasks if t.public_hostname]

    report.checks.append(_check_file_mode())
    report.checks.append(_check_cloudflared_present())

    eff = resolveCloudflareConfig(
        global_cf=global_cfg.tunnel.cloudflare,
        project_cf=project_cfg.tunnel.cloudflare,
        project_id=project_id,
        api_token_override=api_token_override,
    )

    token_check = await _check_token(eff.api_token)
    report.checks.append(token_check)
    if not token_check.ok:
        return report

    report.checks.extend(await _check_account_and_zone(eff.api_token, eff, hostnames))
    # Only short-circuit when the API tier is unreachable — local checks
    # (cloudflared, file mode) failing shouldn't hide DNS info.
    api_failed = any(
        not c.ok and c.name in {"api_token", "account_id", "zones_list"} for c in report.checks
    )
    if api_failed:
        return report

    # Build zone_by_fqdn for collision check.
    zones = await _list_zones(eff.api_token) if (hostnames and eff.api_token) else []
    by_name = {z["name"]: z for z in zones}
    zones_by_fqdn: dict[str, dict] = {}
    for fqdn in hostnames:
        for i in range(len(fqdn.split(".")) - 1):
            candidate = ".".join(fqdn.split(".")[i:])
            if candidate in by_name:
                zones_by_fqdn[fqdn] = by_name[candidate]
                break
    report.checks.extend(
        await _check_dns_collisions(eff.api_token, zones_by_fqdn, hostnames, tunnel_id)
    )

    return report


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------


async def enable(
    *,
    config_path: Path,
    api_token: str | None = None,
    zone_id: str | None = None,
    account_id: str | None = None,
    tasks: list[str] | None = None,
    public_hostnames: dict[str, str] | None = None,
    dry_run: bool = False,
) -> EnableResult:
    """Idempotently set up Cloudflare tunneling for the project at ``config_path``.

    Argument cascade: any explicit kwarg here is written to global config (or
    project config for zone_id/tunnel_name) before resolving. ``api_token`` is
    only ever written to ``~/.taskmux/config.toml`` (mode 0600), never project.
    """
    identity = loadProjectIdentity(config_path)
    project_id = identity.project_id

    # --- 1. write any explicit credentials to global config (token, account_id)
    if not dry_run:
        if api_token:
            updateGlobalConfig({"tunnel.cloudflare.api_token": api_token})
        if account_id:
            updateGlobalConfig({"tunnel.cloudflare.account_id": account_id})

    global_cfg = loadGlobalConfig()
    project_cfg = identity.config

    # --- 2. attach `tunnel = "cloudflare"` to selected tasks (if requested)
    public_hostnames = public_hostnames or {}
    target_tasks = tasks if tasks is not None else list(public_hostnames.keys())
    if target_tasks:
        new_tasks: dict[str, TaskConfig] = {}
        for tname, tcfg in project_cfg.tasks.items():
            if tname in target_tasks:
                hostname = public_hostnames.get(tname) or tcfg.public_hostname
                if not hostname:
                    return EnableResult(
                        ok=False,
                        backend="cloudflare",
                        project_id=project_id,
                        tunnel_name=None,
                        tunnel_id=None,
                        public_urls={},
                        config={},
                        preflight=PreflightReport(),
                        error=(
                            f"Task {tname!r}: no public_hostname provided "
                            "(pass via public_hostnames= or set in taskmux.toml)"
                        ),
                    )
                new_tasks[tname] = tcfg.model_copy(
                    update={"tunnel": TunnelKind.CLOUDFLARE, "public_hostname": hostname}
                )
            else:
                new_tasks[tname] = tcfg
        project_cfg = TaskmuxConfig(
            name=project_cfg.name,
            auto_start=project_cfg.auto_start,
            auto_daemon=project_cfg.auto_daemon,
            auto_inject_agents=project_cfg.auto_inject_agents,
            hooks=project_cfg.hooks,
            worktree=project_cfg.worktree,
            tunnel=project_cfg.tunnel,
            tasks=new_tasks,
        )
        if not dry_run:
            writeConfig(config_path, project_cfg)

    # --- 3. write zone_id at project level if explicitly passed
    if zone_id and not dry_run:
        new_tunnel = TunnelProjectConfig(
            cloudflare=CloudflareTunnelProjectConfig(
                zone_id=zone_id, tunnel_name=project_cfg.tunnel.cloudflare.tunnel_name
            )
        )
        project_cfg = TaskmuxConfig(
            name=project_cfg.name,
            auto_start=project_cfg.auto_start,
            auto_daemon=project_cfg.auto_daemon,
            auto_inject_agents=project_cfg.auto_inject_agents,
            hooks=project_cfg.hooks,
            worktree=project_cfg.worktree,
            tunnel=new_tunnel,
            tasks=project_cfg.tasks,
        )
        writeConfig(config_path, project_cfg)

    # --- 4. resolve effective config
    eff = resolveCloudflareConfig(
        global_cf=global_cfg.tunnel.cloudflare,
        project_cf=project_cfg.tunnel.cloudflare,
        project_id=project_id,
        api_token_override=None,
    )

    # --- 5. auto-resolve missing zone from public_hostname
    cf_tasks = _project_uses_cloudflare(project_cfg)
    hostnames = [t.public_hostname for _, t in cf_tasks if t.public_hostname]
    if eff.api_token and not eff.zone_id and hostnames and not dry_run:
        zone = await _resolve_zone_for_hostname(eff.api_token, hostnames[0])
        if zone is not None:
            updateGlobalConfig({"tunnel.cloudflare.zone_id": zone["id"]})
            global_cfg = loadGlobalConfig()
            eff = resolveCloudflareConfig(
                global_cf=global_cfg.tunnel.cloudflare,
                project_cf=project_cfg.tunnel.cloudflare,
                project_id=project_id,
            )
            eff = EffectiveCloudflareConfig(
                account_id=eff.account_id,
                zone_id=zone["id"],
                tunnel_name=eff.tunnel_name,
                api_token=eff.api_token,
                sources={**eff.sources, "zone_id": "auto"},
            )

    # --- 6. preflight (token, scopes, zone, collisions)
    report = await preflight(
        project_id=project_id,
        project_cfg=project_cfg,
        global_cfg=global_cfg,
    )

    # --- 7. mutating step: ensure tunnel + DNS routes
    tunnel_id: str | None = None
    public_urls: dict[str, str] = {
        name: f"https://{t.public_hostname}/" for name, t in cf_tasks if t.public_hostname
    }

    if not report.ok or dry_run or not eff.api_token or not eff.account_id:
        return EnableResult(
            ok=report.ok and not dry_run,
            backend="cloudflare",
            project_id=project_id,
            tunnel_name=eff.tunnel_name,
            tunnel_id=tunnel_id,
            public_urls=public_urls,
            config=_describe_effective(eff),
            preflight=report,
            error=None if report.ok else "preflight failed",
        )

    tunnel_id, _token = await _ensure_tunnel(eff.api_token, eff.account_id, eff.tunnel_name)
    for fqdn in hostnames:
        ok, detail = await _route_dns(eff.api_token, eff.account_id, tunnel_id, fqdn)
        if not ok:
            report.checks.append(CheckResult(f"dns_route:{fqdn}", False, f"route failed: {detail}"))

    return EnableResult(
        ok=all(c.ok for c in report.checks),
        backend="cloudflare",
        project_id=project_id,
        tunnel_name=eff.tunnel_name,
        tunnel_id=tunnel_id,
        public_urls=public_urls,
        config=_describe_effective(eff),
        preflight=report,
    )


async def disable(
    *,
    config_path: Path,
    prune: bool = False,
) -> dict:
    """Strip `tunnel`/`public_hostname` from every task; optionally remove
    the project's `[tunnel.cloudflare]` block. Daemon resync drops ingress
    on the next reconcile, so we don't touch Cloudflare here."""
    identity = loadProjectIdentity(config_path)
    project_cfg = identity.config
    new_tasks: dict[str, TaskConfig] = {}
    for tname, tcfg in project_cfg.tasks.items():
        if tcfg.tunnel is None:
            new_tasks[tname] = tcfg
            continue
        new_tasks[tname] = tcfg.model_copy(update={"tunnel": None, "public_hostname": None})
    new_tunnel = TunnelProjectConfig() if prune else project_cfg.tunnel
    project_cfg = TaskmuxConfig(
        name=project_cfg.name,
        auto_start=project_cfg.auto_start,
        auto_daemon=project_cfg.auto_daemon,
        auto_inject_agents=project_cfg.auto_inject_agents,
        hooks=project_cfg.hooks,
        worktree=project_cfg.worktree,
        tunnel=new_tunnel,
        tasks=new_tasks,
    )
    writeConfig(config_path, project_cfg)
    return {
        "ok": True,
        "backend": "cloudflare",
        "project_id": identity.project_id,
        "pruned": prune,
        "tasks_disabled": [n for n in new_tasks],
    }


# ---------------------------------------------------------------------------
# Config-shape helpers (CLI/API render these)
# ---------------------------------------------------------------------------


def _mask_token(token: str | None, reveal: bool) -> str | None:
    if token is None:
        return None
    if reveal:
        return token
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}***{token[-4:]}"


def _describe_effective(eff: EffectiveCloudflareConfig) -> dict:
    """Render the effective config for `tunnel config` JSON output."""
    return {
        "account_id": {"value": eff.account_id, "source": eff.sources.get("account_id")},
        "zone_id": {"value": eff.zone_id, "source": eff.sources.get("zone_id")},
        "tunnel_name": {"value": eff.tunnel_name, "source": eff.sources.get("tunnel_name")},
        "api_token": {
            "value": _mask_token(eff.api_token, reveal=False),
            "source": eff.sources.get("api_token"),
            "masked": True,
        },
    }


def describeTunnelConfig(
    *,
    config_path: Path,
    reveal: bool = False,
) -> dict:
    """Render every layer of tunnel config for `taskmux tunnel config`."""
    identity = loadProjectIdentity(config_path)
    global_cfg = loadGlobalConfig()
    eff = resolveCloudflareConfig(
        global_cf=global_cfg.tunnel.cloudflare,
        project_cf=identity.config.tunnel.cloudflare,
        project_id=identity.project_id,
    )
    return {
        "ok": True,
        "backend": "cloudflare",
        "project_id": identity.project_id,
        "config_path": str(config_path),
        "global_config_path": str(globalConfigPath()),
        "effective": _describe_effective(eff),
        "global": {
            "account_id": global_cfg.tunnel.cloudflare.account_id,
            "zone_id": global_cfg.tunnel.cloudflare.zone_id,
            "tunnel_name": global_cfg.tunnel.cloudflare.tunnel_name,
            "api_token": _mask_token(global_cfg.tunnel.cloudflare.api_token, reveal),
            "api_token_env": global_cfg.tunnel.cloudflare.api_token_env,
        },
        "project": {
            "zone_id": identity.config.tunnel.cloudflare.zone_id,
            "tunnel_name": identity.config.tunnel.cloudflare.tunnel_name,
        },
        "tasks": [
            {
                "name": name,
                "tunnel": str(t.tunnel) if t.tunnel else None,
                "host": t.host,
                "public_hostname": t.public_hostname,
                "public_url": (f"https://{t.public_hostname}/" if t.public_hostname else None),
            }
            for name, t in identity.config.tasks.items()
        ],
        "cloudflared_in_path": shutil.which("cloudflared") is not None,
        "config_file_mode_ok": globalConfigModeOk()[0],
    }


def setTunnelConfig(
    *,
    scope: str,
    updates: dict[str, Any],
    config_path: Path | None = None,
) -> dict:
    """Persist a partial config update at the requested scope.

    ``scope`` is ``"global"`` or ``"project"``. Updates use the same dotted
    paths as ``updateGlobalConfig`` (``zone_id``, ``account_id``, etc., or
    nested ``tunnel.cloudflare.zone_id``).
    """
    if scope not in ("global", "project"):
        raise TaskmuxError(
            ErrorCode.INVALID_ARGUMENT,
            detail=f"scope must be 'global' or 'project', got {scope!r}",
        )

    if scope == "global":
        # Allow either flat keys (zone_id) or fully-qualified
        # (tunnel.cloudflare.zone_id). Flat keys are mapped under cloudflare.
        normalised: dict[str, Any] = {}
        for key, value in updates.items():
            if "." in key:
                normalised[key] = value
            elif key in {"account_id", "zone_id", "tunnel_name", "api_token", "api_token_env"}:
                normalised[f"tunnel.cloudflare.{key}"] = value
            else:
                normalised[key] = value
        # writeGlobalConfig chmods 0600 when api_token is present.
        updateGlobalConfig(normalised)
        return {
            "ok": True,
            "scope": "global",
            "config_path": str(globalConfigPath()),
            "updated": list(normalised.keys()),
        }

    if config_path is None:
        raise TaskmuxError(
            ErrorCode.INVALID_ARGUMENT,
            detail="project scope requires a config_path",
        )
    identity = loadProjectIdentity(config_path)
    cf_updates = {k.split(".")[-1]: v for k, v in updates.items() if k != "api_token"}
    if "api_token" in updates or "api_token_env" in cf_updates:
        raise TaskmuxError(
            ErrorCode.CONFIG_VALIDATION,
            detail="api_token cannot be set at project scope (use scope=global)",
        )
    new_cf = identity.config.tunnel.cloudflare.model_copy(update=cf_updates)
    new_tunnel = TunnelProjectConfig(cloudflare=new_cf)
    new_cfg = TaskmuxConfig(
        name=identity.config.name,
        auto_start=identity.config.auto_start,
        auto_daemon=identity.config.auto_daemon,
        auto_inject_agents=identity.config.auto_inject_agents,
        hooks=identity.config.hooks,
        worktree=identity.config.worktree,
        tunnel=new_tunnel,
        tasks=identity.config.tasks,
    )
    writeConfig(config_path, new_cfg)
    return {
        "ok": True,
        "scope": "project",
        "config_path": str(config_path),
        "updated": list(updates.keys()),
    }


__all__ = [
    "CheckResult",
    "EnableResult",
    "PreflightReport",
    "describeTunnelConfig",
    "disable",
    "enable",
    "preflight",
    "setTunnelConfig",
]
