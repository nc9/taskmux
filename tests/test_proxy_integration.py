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


def test_proxy_returns_502_for_unmatched_route(tmp_path):
    from taskmux.proxy import ProxyServer

    proxy_port = _free_port()
    cert, key = _make_cert(tmp_path / "certs" / "demo", "demo")

    async def run() -> int:
        proxy = ProxyServer(https_port=proxy_port, bind="127.0.0.1")
        proxy.register_project("demo", cert, key)
        # Note: NO set_route — so any host should 502.
        await proxy.start()
        try:
            ssl_ctx = ssl.create_default_context(cafile=str(_ca_root()))
            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                proxy_port,
                ssl=ssl_ctx,
                server_hostname="ghost.demo.localhost",
            )
            writer.write(
                b"GET / HTTP/1.1\r\nHost: ghost.demo.localhost\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            raw = await reader.read()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            status_line = raw.split(b"\r\n", 1)[0].decode()
            return int(status_line.split()[1])
        finally:
            await proxy.stop()

    assert asyncio.run(run()) == 502
