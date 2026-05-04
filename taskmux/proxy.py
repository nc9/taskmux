"""HTTPS reverse proxy: {host}.{project}.localhost -> upstream port.

Daemon owns the lifecycle. Routes and SNI certs are mutated via:
  - register_project(project, cert, key)
  - unregister_project(project)
  - set_route(project, host, port, task=None)
  - drop_route(project, host)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web

# RFC 7230 hop-by-hop headers — must not be forwarded.
_HOP_BY_HOP = frozenset(
    h.lower()
    for h in (
        "Connection",
        "Keep-Alive",
        "Proxy-Authenticate",
        "Proxy-Authorization",
        "TE",
        "Trailers",
        "Transfer-Encoding",
        "Upgrade",
    )
)


def _parseHost(host_header: str | None) -> tuple[str, str] | None:
    """Parse a host header into (project, host).

    Shapes accepted (all under `.localhost`):
      - `{project}.localhost`            → (project, "")        # apex
      - `{host}.{project}.localhost`     → (project, host)      # specific
      - `a.b.{project}.localhost`        → (project, "a.b")     # multi-label
    Returns None for `localhost` alone or empty input.
    """
    if not host_header:
        return None
    bare = host_header.split(":", 1)[0].lower().rstrip(".")
    if not bare.endswith(".localhost"):
        return None
    labels = bare[: -len(".localhost")].split(".")
    # Empty bare ("" or ".localhost") would yield [""] — reject.
    if not labels or labels == [""]:
        return None
    # Last label = project, everything before it = host. Apex → host is "".
    project = labels[-1]
    host = ".".join(labels[:-1])
    return project, host


@dataclass
class _ProjectCert:
    cert: Path
    key: Path
    ctx: ssl.SSLContext


class ProxyServer:
    """HTTPS reverse proxy fronting all taskmux-managed projects."""

    def __init__(
        self,
        https_port: int = 443,
        bind: str = "127.0.0.1",
        socks: list[socket.socket] | None = None,
    ) -> None:
        self.https_port = https_port
        self.bind = bind
        # Optional pre-bound listening sockets. The daemon binds :443 as root,
        # drops privileges, and hands the sockets here. Multiple sockets are
        # used for dual-stack (v4 + v6) so macOS getaddrinfo's `*.localhost`
        # → ::1 mapping doesn't dead-end on a v4-only listener.
        self._socks: list[socket.socket] = list(socks) if socks else []
        self.logger = logging.getLogger("taskmux-daemon.proxy")
        self._routes: dict[tuple[str, str], int] = {}
        # Reverse map for diagnostics + on_upstream_dead notifications. Aliases
        # set a route without a task name; the diagnostic falls back to the
        # host display in that case.
        self._route_tasks: dict[tuple[str, str], str] = {}
        self._projects: dict[str, _ProjectCert] = {}
        self._runner: web.AppRunner | None = None
        self._sites: list[web.BaseSite] = []
        self._default_ctx: ssl.SSLContext | None = None
        # Hook fired when forwarding hits a connection-refused error. The
        # daemon wires this to supervisor.notify_upstream_dead so the cache
        # invalidates immediately on the next status / health tick.
        self.on_upstream_dead: Callable[[str, str, str | None], None] | None = None

    # ---- public mutators ----

    def register_project(self, project: str, cert: Path, key: Path) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        self._projects[project] = _ProjectCert(cert=cert, key=key, ctx=ctx)
        # Boot the default ctx with the first cert if not yet set.
        if self._default_ctx is None:
            self._default_ctx = ctx

    def unregister_project(self, project: str) -> None:
        self._projects.pop(project, None)
        for key in [k for k in self._routes if k[0] == project]:
            self._routes.pop(key, None)
            self._route_tasks.pop(key, None)

    def set_route(self, project: str, host: str, port: int, task: str | None = None) -> None:
        self._routes[(project, host)] = port
        if task is not None:
            self._route_tasks[(project, host)] = task
        else:
            self._route_tasks.pop((project, host), None)

    def drop_route(self, project: str, host: str) -> None:
        self._routes.pop((project, host), None)
        self._route_tasks.pop((project, host), None)

    def routes_snapshot(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for (project, host), port in self._routes.items():
            out.setdefault(project, {})[host] = port
        return out

    # ---- lifecycle ----

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()

        if self._default_ctx is None:
            # No project certs yet — server can't start TLS without a cert.
            # Daemon must register at least one project before calling start().
            raise RuntimeError(
                "proxy.start() called with no registered projects (no SNI default cert)"
            )

        # SNI dispatch: swap context to match servername.
        self._default_ctx.sni_callback = self._sni_callback  # type: ignore[attr-defined]

        if self._socks:
            for sk in self._socks:
                site = web.SockSite(
                    self._runner,
                    sk,
                    ssl_context=self._default_ctx,
                )
                await site.start()
                self._sites.append(site)
                fam = "v6" if sk.family == socket.AF_INET6 else "v4"
                self.logger.info(
                    f"Proxy listening on https://{self.bind}:{self.https_port} ({fam})"
                )
        else:
            site = web.TCPSite(
                self._runner,
                host=self.bind,
                port=self.https_port,
                ssl_context=self._default_ctx,
            )
            await site.start()
            self._sites.append(site)
            self.logger.info(f"Proxy listening on https://{self.bind}:{self.https_port}")

    async def stop(self) -> None:
        for site in self._sites:
            await site.stop()
        self._sites = []
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ---- SNI ----

    def _sni_callback(
        self,
        ssl_socket: ssl.SSLObject,
        servername: str | None,
        _ssl_context: ssl.SSLContext,
    ) -> None:
        if not servername:
            return
        parsed = _parseHost(servername)
        if parsed is None:
            return
        project, _host = parsed
        proj = self._projects.get(project)
        if proj is not None:
            ssl_socket.context = proj.ctx

    # ---- request dispatch ----

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        parsed = _parseHost(request.headers.get("Host"))
        if parsed is None:
            return web.Response(status=400, text="invalid host header\n")
        project, host = parsed
        # Lookup precedence: exact (project, host) → wildcard (project, "*").
        # Apex queries arrive with host == "" and never fall through to "*".
        port = self._routes.get((project, host))
        matched_host = host
        if port is None and host != "":
            port = self._routes.get((project, "*"))
            if port is not None:
                matched_host = "*"
        if port is None:
            display = f"{host}.{project}.localhost" if host else f"{project}.localhost"
            # 503 (not 502): nothing is wired here, so it's a configuration /
            # lifecycle issue the user should resolve, not a transient
            # upstream failure.
            return web.Response(
                status=503,
                text=(
                    f"taskmux: no upstream for {display}\n"
                    f"  hint: run `taskmux start <task>` for the host '{host or '@'}' "
                    f"in project '{project}'.\n"
                ),
            )

        task = self._route_tasks.get((project, matched_host))
        # WebSocket upgrade?
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_ws(request, port)
        return await self._proxy_http(request, port, project=project, task=task)

    async def _proxy_http(
        self,
        request: web.Request,
        port: int,
        *,
        project: str | None = None,
        task: str | None = None,
    ) -> web.StreamResponse:
        # Use `localhost` not `127.0.0.1` so the resolver picks IPv4 / IPv6 to
        # match whichever family the upstream actually bound (Vite + many Node
        # tools default to ::1; Python http.server defaults to 0.0.0.0).
        target = f"http://localhost:{port}{request.rel_url.raw_path_qs}"
        headers = _filter_hop(request.headers)

        timeout = aiohttp.ClientTimeout(total=None, sock_read=None, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            try:
                async with session.request(
                    request.method,
                    target,
                    headers=headers,
                    data=request.content if request.body_exists else None,
                    allow_redirects=False,
                ) as upstream:
                    response = web.StreamResponse(
                        status=upstream.status,
                        reason=upstream.reason,
                        headers=_filter_hop(upstream.headers),
                    )
                    await response.prepare(request)
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
                    await response.write_eof()
                    return response
            except aiohttp.ClientConnectorError as e:
                # Upstream is wired but not answering — task crashed, hung, or
                # never bound the port. Notify the supervisor so the cache
                # invalidates immediately and the next health tick acts on
                # fresh state. Return 503 with a directly-actionable hint.
                cb = self.on_upstream_dead
                if cb is not None and project is not None:
                    try:
                        cb(project, "", task)
                    except Exception:  # noqa: BLE001
                        self.logger.warning("on_upstream_dead callback raised", exc_info=True)
                label = task or f"port {port}"
                hint = (
                    f"`taskmux restart {task}`"
                    if task
                    else f"checking the task bound to port {port}"
                )
                return web.Response(
                    status=503,
                    text=(
                        f"taskmux: upstream {label} not responding on port {port}\n"
                        f"  hint: try {hint}.\n"
                        f"  detail: {e}\n"
                    ),
                )
            except aiohttp.ClientError as e:
                return web.Response(status=502, text=f"upstream error: {e}\n")

    async def _proxy_ws(self, request: web.Request, port: int) -> web.StreamResponse:
        upstream_url = f"ws://localhost:{port}{request.rel_url.raw_path_qs}"
        client_ws = web.WebSocketResponse()
        await client_ws.prepare(request)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.ws_connect(
                    upstream_url,
                    headers=_filter_hop(request.headers),
                ) as upstream_ws,
            ):
                await asyncio.gather(
                    _ws_pipe(client_ws, upstream_ws),
                    _ws_pipe(upstream_ws, client_ws),
                    return_exceptions=True,
                )
        except aiohttp.ClientError as e:
            self.logger.warning(f"ws upstream error: {e}")

        return client_ws


def _filter_hop(headers) -> dict[str, str]:  # noqa: ANN001
    """Strip hop-by-hop headers per RFC 7230."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


async def _ws_pipe(src, dst) -> None:  # noqa: ANN001
    """Forward WS messages from src to dst until either side closes."""
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
            await dst.close()
            return
