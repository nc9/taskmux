"""HTTPS reverse proxy: {host}.{project}.localhost -> upstream port.

Daemon owns the lifecycle. Routes and SNI certs are mutated via:
  - register_project(project, cert, key)
  - unregister_project(project)
  - set_route(project, host, port)
  - drop_route(project, host)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
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
    """Parse '{host}.{project}.localhost[:port]' into (project, host)."""
    if not host_header:
        return None
    bare = host_header.split(":", 1)[0].lower().rstrip(".")
    if not bare.endswith(".localhost"):
        return None
    labels = bare[: -len(".localhost")].split(".")
    if len(labels) < 2:
        return None
    # Last label = project, everything before it = host (allow 'web-1.api' too)
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
        sock: socket.socket | None = None,
    ) -> None:
        self.https_port = https_port
        self.bind = bind
        # Optional pre-bound listening socket. Used when the daemon binds :443
        # as root and then drops privileges before starting the listener.
        self._sock = sock
        self.logger = logging.getLogger("taskmux-daemon.proxy")
        self._routes: dict[tuple[str, str], int] = {}
        self._projects: dict[str, _ProjectCert] = {}
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        self._default_ctx: ssl.SSLContext | None = None

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

    def set_route(self, project: str, host: str, port: int) -> None:
        self._routes[(project, host)] = port

    def drop_route(self, project: str, host: str) -> None:
        self._routes.pop((project, host), None)

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

        if self._sock is not None:
            self._site = web.SockSite(
                self._runner,
                self._sock,
                ssl_context=self._default_ctx,
            )
        else:
            self._site = web.TCPSite(
                self._runner,
                host=self.bind,
                port=self.https_port,
                ssl_context=self._default_ctx,
            )
        await self._site.start()
        self.logger.info(f"Proxy listening on https://{self.bind}:{self.https_port}")

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
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
        port = self._routes.get((project, host))
        if port is None:
            return web.Response(
                status=502,
                text=f"no upstream for {host}.{project}.localhost\n",
            )

        # WebSocket upgrade?
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._proxy_ws(request, port)
        return await self._proxy_http(request, port)

    async def _proxy_http(self, request: web.Request, port: int) -> web.StreamResponse:
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
