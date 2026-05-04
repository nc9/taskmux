"""End-to-end proxy test: mkcert → ProxyServer → upstream → trusted HTTPS round-trip.

Skipped when mkcert is not on PATH (CI without mkcert installed).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

mkcert_missing = shutil.which("mkcert") is None
pytestmark = pytest.mark.skipif(mkcert_missing, reason="mkcert not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_cert(out_dir: Path, project: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cert = out_dir / "cert.pem"
    key = out_dir / "key.pem"
    subprocess.run(
        [
            "mkcert",
            "-cert-file",
            str(cert),
            "-key-file",
            str(key),
            f"*.{project}.localhost",
            "localhost",
            "127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _ca_root() -> Path:
    out = subprocess.run(["mkcert", "-CAROOT"], capture_output=True, text=True, check=True)
    return Path(out.stdout.strip()) / "rootCA.pem"


def test_proxy_routes_https_traffic_end_to_end(tmp_path):
    from aiohttp import web

    from taskmux.proxy import ProxyServer

    upstream_port = _free_port()
    proxy_port = _free_port()
    cert, key = _make_cert(tmp_path / "certs" / "demo", "demo")

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.json_response({"path": str(request.rel_url), "host": request.host})

    async def run() -> tuple[int, dict]:
        upstream = web.Application()
        upstream.router.add_get("/{tail:.*}", upstream_handler)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host="127.0.0.1", port=upstream_port)
        await site.start()

        proxy = ProxyServer(https_port=proxy_port, bind="127.0.0.1")
        proxy.register_project("demo", cert, key)
        proxy.set_route("demo", "api", upstream_port)
        await proxy.start()

        try:
            ssl_ctx = ssl.create_default_context(cafile=str(_ca_root()))
            # 'api.demo.localhost' resolves to 127.0.0.1 in modern resolvers but
            # we don't rely on DNS — connect to 127.0.0.1 with SNI=api.demo.localhost.
            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                proxy_port,
                ssl=ssl_ctx,
                server_hostname="api.demo.localhost",
            )
            req = (
                b"GET /hello?x=1 HTTP/1.1\r\nHost: api.demo.localhost\r\nConnection: close\r\n\r\n"
            )
            writer.write(req)
            await writer.drain()
            raw = await reader.read()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

            head, _, body = raw.partition(b"\r\n\r\n")
            status_line = head.split(b"\r\n", 1)[0].decode()
            return int(status_line.split()[1]), {"raw": body.decode()}
        finally:
            await proxy.stop()
            await runner.cleanup()

    if sys.platform == "win32":
        pytest.skip("event-loop policy specifics on windows")

    status, payload = asyncio.run(run())
    assert status == 200
    assert "/hello?x=1" in payload["raw"]
    assert "api.demo.localhost" in payload["raw"]


def test_proxy_serves_both_v4_and_v6_loopback(tmp_path):
    """Regression: macOS getaddrinfo maps *.localhost to ::1 first, so the
    proxy must answer on the v6 loopback as well as v4."""
    from aiohttp import web

    from taskmux.proxy import ProxyServer

    if sys.platform == "win32":
        pytest.skip("event-loop policy specifics on windows")

    upstream_port = _free_port()
    proxy_port = _free_port()
    cert, key = _make_cert(tmp_path / "certs" / "demo", "demo")

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    def _bind(family: int, addr: str, port: int) -> socket.socket:
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((addr, port))
        s.listen(64)
        s.setblocking(False)
        return s

    async def run() -> tuple[int, int]:
        upstream = web.Application()
        upstream.router.add_get("/{tail:.*}", upstream_handler)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        upsite = web.TCPSite(runner, host="127.0.0.1", port=upstream_port)
        await upsite.start()

        try:
            v4 = _bind(socket.AF_INET, "127.0.0.1", proxy_port)
            v6 = _bind(socket.AF_INET6, "::1", proxy_port)
        except OSError:
            pytest.skip("v6 loopback not available in this environment")

        proxy = ProxyServer(https_port=proxy_port, bind="127.0.0.1", socks=[v4, v6])
        proxy.register_project("demo", cert, key)
        proxy.set_route("demo", "api", upstream_port)
        await proxy.start()

        async def _hit(host: str) -> int:
            ssl_ctx = ssl.create_default_context(cafile=str(_ca_root()))
            reader, writer = await asyncio.open_connection(
                host,
                proxy_port,
                ssl=ssl_ctx,
                server_hostname="api.demo.localhost",
            )
            writer.write(b"GET / HTTP/1.1\r\nHost: api.demo.localhost\r\nConnection: close\r\n\r\n")
            await writer.drain()
            raw = await reader.read()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return int(raw.split(b"\r\n", 1)[0].split()[1])

        try:
            v4_status = await _hit("127.0.0.1")
            v6_status = await _hit("::1")
            return v4_status, v6_status
        finally:
            await proxy.stop()
            await runner.cleanup()

    v4_status, v6_status = asyncio.run(run())
    assert v4_status == 200
    assert v6_status == 200


async def _send_request_via_proxy(proxy_port: int, hostname: str) -> tuple[int, bytes]:
    """Open TLS to the proxy with the given SNI/Host and read the full response."""
    ssl_ctx = ssl.create_default_context(cafile=str(_ca_root()))
    reader, writer = await asyncio.open_connection(
        "127.0.0.1",
        proxy_port,
        ssl=ssl_ctx,
        server_hostname=hostname,
    )
    writer.write(f"GET / HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    status_line = raw.split(b"\r\n", 1)[0].decode()
    return int(status_line.split()[1]), raw


def test_proxy_returns_503_with_diagnostic_body_for_unmatched_route(tmp_path):
    from taskmux.proxy import ProxyServer

    proxy_port = _free_port()
    cert, key = _make_cert(tmp_path / "certs" / "demo", "demo")

    async def run() -> tuple[int, bytes]:
        proxy = ProxyServer(https_port=proxy_port, bind="127.0.0.1")
        proxy.register_project("demo", cert, key)
        # Note: NO set_route — so any host should 503 with a hint.
        await proxy.start()
        try:
            return await _send_request_via_proxy(proxy_port, "ghost.demo.localhost")
        finally:
            await proxy.stop()

    status, raw = asyncio.run(run())
    assert status == 503
    body = raw.split(b"\r\n\r\n", 1)[1]
    assert b"taskmux: no upstream" in body
    assert b"taskmux start" in body


def test_proxy_returns_503_and_notifies_on_econnrefused(tmp_path):
    """Route is wired to a port nothing is listening on. Forwarding hits
    ECONNREFUSED → proxy returns 503 with a `taskmux restart` hint and fires
    on_upstream_dead with the task name."""
    from taskmux.proxy import ProxyServer

    proxy_port = _free_port()
    cert, key = _make_cert(tmp_path / "certs" / "demo", "demo")

    # A port we own briefly to learn its number, then close so connect refuses.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    notified: list[tuple[str, str, str | None]] = []

    async def run() -> tuple[int, bytes]:
        proxy = ProxyServer(https_port=proxy_port, bind="127.0.0.1")
        proxy.register_project("demo", cert, key)
        proxy.on_upstream_dead = lambda p, h, t: notified.append((p, h, t))
        proxy.set_route("demo", "api", dead_port, task="api-task")
        await proxy.start()
        try:
            return await _send_request_via_proxy(proxy_port, "api.demo.localhost")
        finally:
            await proxy.stop()

    status, raw = asyncio.run(run())
    assert status == 503
    body = raw.split(b"\r\n\r\n", 1)[1]
    assert b"taskmux: upstream api-task not responding" in body
    assert b"taskmux restart api-task" in body
    assert notified and notified[0][2] == "api-task"
