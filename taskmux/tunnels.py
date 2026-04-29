"""Pluggable public-tunnel backends.

A `TunnelBackend` makes one or more taskmux services reachable on the public
internet under a `public_hostname` while leaving the local `.localhost` URL
unchanged. The daemon owns a per-(project, backend) instance, calls `sync()`
on every route reconcile (additive: tunnels run alongside the etc_hosts
resolver, never replace it), and `clear()` on shutdown.

Mappings flow:

    (public_hostname, internal_fqdn, proxy_port)

Public traffic enters the backend's edge network, lands on
`https://localhost:<proxy_port>` with `Host: <internal_fqdn>`, and the
existing taskmux proxy routes by Host header — no proxy code changes.

Backends:

  - cloudflare — remote-managed Cloudflare Tunnel. We drive the Cloudflare
    REST API directly: create-or-load a `cfd_tunnel`, PUT ingress, upsert
    DNS routes, run `cloudflared` as a child process with the returned
    token. State (tunnel_id + token) is cached at
    ~/.taskmux/tunnels/cloudflare/<tunnel_name>.json so daemon restarts
    are cheap.
  - noop — record the mapping for status display and otherwise do nothing.
    Use for self-hosted tunnels (frp / sish / Caddy) where you wire the
    public name to the proxy yourself.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable

import aiohttp

from .errors import ErrorCode, TaskmuxError
from .paths import tunnelStateDir

TunnelMapping = tuple[str, str, int]

_CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"


@runtime_checkable
class TunnelBackend(Protocol):
    name: str

    async def sync(self, mappings: list[TunnelMapping]) -> None: ...
    async def clear(self) -> None: ...
    def public_url(self, public_hostname: str) -> str: ...
    def status(self) -> dict: ...


class CloudflareTunnelBackend:
    name = "cloudflare"

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        zone_id: str,
        tunnel_name: str,
        proxy_port: int,
        cloudflared_bin: str = "cloudflared",
        state_path: Path | None = None,
    ) -> None:
        self.account_id = account_id
        self.api_token = api_token
        self.zone_id = zone_id
        self.tunnel_name = tunnel_name
        self.proxy_port = proxy_port
        self.cloudflared_bin = cloudflared_bin
        self.state_path = state_path or (tunnelStateDir("cloudflare") / f"{tunnel_name}.json")
        self._tunnel_id: str | None = None
        self._tunnel_token: str | None = None
        self._cloudflared_proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._last_sync_ok: bool = False
        self._last_error: str | None = None
        self._last_mapping_count: int = 0
        self.logger = logging.getLogger("taskmux-daemon.tunnels.cloudflare")
        self._load_state()

    async def sync(self, mappings: list[TunnelMapping]) -> None:
        async with self._lock:
            try:
                await self._sync_locked(mappings)
                self._last_sync_ok = True
                self._last_error = None
                self._last_mapping_count = len(mappings)
            except Exception as e:  # noqa: BLE001
                self._last_sync_ok = False
                self._last_error = str(e)
                raise

    async def clear(self) -> None:
        async with self._lock:
            if self._tunnel_id:
                with contextlib.suppress(Exception):
                    await self._put_ingress([{"service": "http_status:404"}])
            await self._stop_cloudflared()
            self._last_sync_ok = False
            self._last_mapping_count = 0

    def public_url(self, public_hostname: str) -> str:
        return f"https://{public_hostname}/"

    def status(self) -> dict:
        proc = self._cloudflared_proc
        running = proc is not None and proc.returncode is None
        return {
            "backend": self.name,
            "tunnel_name": self.tunnel_name,
            "tunnel_id": self._tunnel_id,
            "cloudflared_running": running,
            "cloudflared_pid": proc.pid if running and proc else None,
            "last_sync_ok": self._last_sync_ok,
            "last_error": self._last_error,
            "mappings": self._last_mapping_count,
        }

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        with contextlib.suppress(Exception):
            data = json.loads(self.state_path.read_text())
            self._tunnel_id = data.get("tunnel_id")
            self._tunnel_token = data.get("token")

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "tunnel_id": self._tunnel_id,
                "tunnel_name": self.tunnel_name,
                "token": self._tunnel_token,
            }
        )
        self.state_path.write_text(payload)
        with contextlib.suppress(OSError):
            os.chmod(self.state_path, 0o600)

    def _build_ingress(self, mappings: list[TunnelMapping]) -> list[dict]:
        ordered = sorted(mappings, key=lambda m: m[0])
        ingress: list[dict] = []
        for public_host, internal_fqdn, port in ordered:
            ingress.append(
                {
                    "hostname": public_host,
                    "service": f"https://localhost:{port}",
                    "originRequest": {
                        "httpHostHeader": internal_fqdn,
                        "originServerName": internal_fqdn,
                        "noTLSVerify": True,
                    },
                }
            )
        ingress.append({"service": "http_status:404"})
        return ingress

    async def _api(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        **kwargs: object,
    ) -> object:
        url = f"{_CLOUDFLARE_API}{path}"
        async with session.request(method, url, **kwargs) as resp:  # type: ignore[arg-type]
            text = await resp.text()
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError as e:
                raise TaskmuxError(
                    ErrorCode.INTERNAL,
                    detail=f"Cloudflare API {method} {path}: non-JSON response: {text[:200]}",
                ) from e
            if resp.status >= 400 or not payload.get("success", False):
                errs = "; ".join(str(e.get("message", "?")) for e in payload.get("errors", []))
                raise TaskmuxError(
                    ErrorCode.INTERNAL,
                    detail=(
                        f"Cloudflare API {method} {path} failed "
                        f"(HTTP {resp.status}): {errs or 'no error detail'}"
                    ),
                )
            return payload.get("result")

    async def _ensure_tunnel(self, session: aiohttp.ClientSession) -> tuple[str, str]:
        if self._tunnel_id and self._tunnel_token:
            try:
                await self._api(
                    session,
                    "GET",
                    f"/accounts/{self.account_id}/cfd_tunnel/{self._tunnel_id}",
                )
                return self._tunnel_id, self._tunnel_token
            except TaskmuxError:
                self._tunnel_id = None
                self._tunnel_token = None

        existing = await self._api(
            session,
            "GET",
            f"/accounts/{self.account_id}/cfd_tunnel",
            params={"name": self.tunnel_name, "is_deleted": "false"},
        )
        if isinstance(existing, list) and existing:
            tid = existing[0]["id"]
        else:
            created = await self._api(
                session,
                "POST",
                f"/accounts/{self.account_id}/cfd_tunnel",
                json={"name": self.tunnel_name, "config_src": "cloudflare"},
            )
            assert isinstance(created, dict)
            tid = created["id"]

        token_result = await self._api(
            session, "GET", f"/accounts/{self.account_id}/cfd_tunnel/{tid}/token"
        )
        token = token_result if isinstance(token_result, str) else str(token_result)
        self._tunnel_id = tid
        self._tunnel_token = token
        self._save_state()
        self.logger.info(f"Cloudflare tunnel {self.tunnel_name} ready (id {tid})")
        return tid, token

    async def _put_ingress(self, ingress: list[dict]) -> None:
        if not self._tunnel_id:
            return
        headers = {"Authorization": f"Bearer {self.api_token}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            await self._api(
                session,
                "PUT",
                f"/accounts/{self.account_id}/cfd_tunnel/{self._tunnel_id}/configurations",
                json={"config": {"ingress": ingress}},
            )

    async def _sync_locked(self, mappings: list[TunnelMapping]) -> None:
        headers = {"Authorization": f"Bearer {self.api_token}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            tid, token = await self._ensure_tunnel(session)
            await self._api(
                session,
                "PUT",
                f"/accounts/{self.account_id}/cfd_tunnel/{tid}/configurations",
                json={"config": {"ingress": self._build_ingress(mappings)}},
            )
            for public_host, _internal, _port in mappings:
                try:
                    await self._api(
                        session,
                        "POST",
                        f"/accounts/{self.account_id}/cfd_tunnel/{tid}/routes/dns",
                        json={"hostname": public_host},
                    )
                except TaskmuxError as e:
                    msg = str(e.details.get("detail", "")).lower()
                    if "already exists" in msg or "duplicate" in msg:
                        continue
                    raise

        if mappings:
            await self._ensure_cloudflared(token)
        else:
            await self._stop_cloudflared()

    async def _ensure_cloudflared(self, token: str) -> None:
        proc = self._cloudflared_proc
        if proc is not None and proc.returncode is None:
            return
        if shutil.which(self.cloudflared_bin) is None:
            raise TaskmuxError(
                ErrorCode.INTERNAL,
                detail=(
                    f"`{self.cloudflared_bin}` not found in PATH. Install "
                    "cloudflared from https://developers.cloudflare.com/"
                    "cloudflare-one/connections/connect-networks/downloads/."
                ),
            )
        log_path = tunnelStateDir("cloudflare") / f"{self.tunnel_name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = log_path.open("ab")
        self._cloudflared_proc = await asyncio.create_subprocess_exec(
            self.cloudflared_bin,
            "tunnel",
            "--no-autoupdate",
            "run",
            "--token",
            token,
            stdout=log_f,
            stderr=log_f,
            stdin=asyncio.subprocess.DEVNULL,
        )
        self.logger.info(f"Started cloudflared (pid {self._cloudflared_proc.pid}) → {log_path}")

    async def _stop_cloudflared(self) -> None:
        proc = self._cloudflared_proc
        if proc is None or proc.returncode is not None:
            self._cloudflared_proc = None
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        self._cloudflared_proc = None


class NoopTunnelBackend:
    name = "noop"

    def __init__(self) -> None:
        self.logger = logging.getLogger("taskmux-daemon.tunnels.noop")
        self._mappings: list[TunnelMapping] = []

    async def sync(self, mappings: list[TunnelMapping]) -> None:
        self._mappings = list(mappings)
        if mappings:
            self.logger.info(f"noop tunnel: {len(mappings)} mapping(s) — relying on external infra")

    async def clear(self) -> None:
        self._mappings = []

    def public_url(self, public_hostname: str) -> str:
        return f"https://{public_hostname}/"

    def status(self) -> dict:
        return {
            "backend": self.name,
            "mappings": len(self._mappings),
            "last_sync_ok": True,
            "last_error": None,
        }


__all__ = [
    "CloudflareTunnelBackend",
    "NoopTunnelBackend",
    "TunnelBackend",
    "TunnelMapping",
]
