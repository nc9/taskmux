"""Tests for the in-process DNS server."""

from __future__ import annotations

import asyncio
import socket

from dnslib import QTYPE, RCODE, DNSRecord

from taskmux.dns_server import DnsServer


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _query(port: int, name: str, qtype: int = QTYPE.A, timeout: float = 1.0) -> DNSRecord:
    q = DNSRecord.question(name, qtype=QTYPE[qtype])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(q.pack(), ("127.0.0.1", port))
        data, _ = s.recvfrom(4096)
    return DNSRecord.parse(data)


async def _spawn(tld: str = "localhost") -> tuple[DnsServer, int]:
    port = _free_udp_port()
    srv = DnsServer(port=port, tld=tld)
    await srv.start()
    return srv, port


def test_explicit_mapping_returns_a():
    async def go():
        srv, port = await _spawn()
        try:
            srv.update([("api.demo.localhost", "127.0.0.1")])
            return await asyncio.to_thread(_query, port, "api.demo.localhost")
        finally:
            await srv.stop()

    rec = asyncio.run(go())
    assert rec.header.rcode == RCODE.NOERROR
    assert len(rec.rr) == 1
    assert rec.rr[0].rtype == QTYPE.A
    assert str(rec.rr[0].rdata) == "127.0.0.1"


def test_unmapped_name_in_tld_uses_catch_all():
    async def go():
        srv, port = await _spawn()
        try:
            srv.update([])
            return await asyncio.to_thread(_query, port, "ghost.unknown.localhost")
        finally:
            await srv.stop()

    rec = asyncio.run(go())
    assert rec.header.rcode == RCODE.NOERROR
    assert len(rec.rr) == 1
    assert str(rec.rr[0].rdata) == "127.0.0.1"


def test_bare_tld_resolves():
    async def go():
        srv, port = await _spawn()
        try:
            return await asyncio.to_thread(_query, port, "localhost")
        finally:
            await srv.stop()

    rec = asyncio.run(go())
    assert rec.header.rcode == RCODE.NOERROR
    assert str(rec.rr[0].rdata) == "127.0.0.1"


def test_aaaa_returns_ipv6_loopback():
    async def go():
        srv, port = await _spawn()
        try:
            return await asyncio.to_thread(_query, port, "api.demo.localhost", QTYPE.AAAA)
        finally:
            await srv.stop()

    rec = asyncio.run(go())
    assert rec.header.rcode == RCODE.NOERROR
    assert len(rec.rr) == 1
    assert rec.rr[0].rtype == QTYPE.AAAA
    assert str(rec.rr[0].rdata) == "::1"


def test_out_of_zone_returns_refused():
    async def go():
        srv, port = await _spawn()
        try:
            return await asyncio.to_thread(_query, port, "example.com")
        finally:
            await srv.stop()

    rec = asyncio.run(go())
    assert rec.header.rcode == RCODE.REFUSED
    assert len(rec.rr) == 0


def test_update_takes_effect_for_next_query():
    async def go():
        srv, port = await _spawn()
        try:
            srv.update([("api.demo.localhost", "10.0.0.1")])
            first = await asyncio.to_thread(_query, port, "api.demo.localhost")
            srv.update([("api.demo.localhost", "10.0.0.2")])
            second = await asyncio.to_thread(_query, port, "api.demo.localhost")
            return first, second
        finally:
            await srv.stop()

    first, second = asyncio.run(go())
    assert str(first.rr[0].rdata) == "10.0.0.1"
    assert str(second.rr[0].rdata) == "10.0.0.2"


def test_case_insensitive_lookup():
    async def go():
        srv, port = await _spawn()
        try:
            srv.update([("api.demo.localhost", "127.0.0.1")])
            return await asyncio.to_thread(_query, port, "API.Demo.LOCALHOST")
        finally:
            await srv.stop()

    rec = asyncio.run(go())
    assert rec.header.rcode == RCODE.NOERROR
    assert str(rec.rr[0].rdata) == "127.0.0.1"


def test_handle_malformed_returns_formerr():
    srv = DnsServer()
    bad = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    out = srv.handle(bad)
    assert isinstance(out, bytes)
