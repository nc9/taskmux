"""Tiny in-process DNS server for dynamic *.{tld} resolution.

Bound to loopback, this UDP server owns a TLD (default `.localhost`) and
answers A/AAAA queries from an in-memory map that the daemon updates as
projects/tasks come and go. Unmapped names within the managed TLD get a
catch-all 127.0.0.1 / ::1 answer so the resolver matches RFC 6761 semantics
for `.localhost`. Queries outside the managed TLD are REFUSED so the OS
resolver falls back to its normal behaviour.

The server is wired into the daemon's asyncio loop via `DatagramProtocol`
and registers as a HostResolver implementation in `host_resolver.py`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSRecord
from dnslib.dns import CLASS

from .host_resolver import HostMapping

_DEFAULT_TTL = 60


def _normalize(name: str) -> str:
    """Lowercase + strip trailing dot."""
    return name.rstrip(".").lower()


class DnsServer:
    """Loopback UDP DNS server with a live in-memory hostname map.

    Owns a single TLD (e.g. `localhost`); answers any query for the bare TLD,
    a name in the explicit map, OR any unmapped subdomain in that TLD with
    the catch-all IP. Queries for other domains return REFUSED.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5353,
        tld: str = "localhost",
        catch_all_ipv4: str = "127.0.0.1",
        catch_all_ipv6: str = "::1",
    ) -> None:
        self.host = host
        self.port = port
        self.tld = _normalize(tld)
        self.catch_all_ipv4 = catch_all_ipv4
        self.catch_all_ipv6 = catch_all_ipv6
        self.logger = logging.getLogger("taskmux-daemon.dns")
        self._map: dict[str, str] = {}
        self._transport: asyncio.DatagramTransport | None = None

    # ---- lifecycle ----

    async def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        loop = loop or asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self),
            local_addr=(self.host, self.port),
        )
        self._transport = transport
        self.logger.info(f"DNS server listening on {self.host}:{self.port} (.{self.tld})")

    async def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    # ---- map updates ----

    def update(self, mappings: list[HostMapping]) -> None:
        """Atomically replace the explicit hostname map."""
        self._map = {_normalize(fqdn): ip for fqdn, ip in mappings}
        self.logger.debug(f"DNS map updated: {len(self._map)} explicit entries")

    def snapshot(self) -> dict[str, str]:
        return dict(self._map)

    # ---- query handling ----

    def _is_managed(self, name: str) -> bool:
        return name == self.tld or name.endswith("." + self.tld)

    def _resolve(self, qname: str, qtype: int) -> tuple[int, str | None]:
        """Return (rcode, ip-or-None). REFUSED for out-of-zone names."""
        name = _normalize(qname)
        if not self._is_managed(name):
            return RCODE.REFUSED, None
        if qtype == QTYPE.A:
            ip = self._map.get(name, self.catch_all_ipv4)
            return RCODE.NOERROR, ip
        if qtype == QTYPE.AAAA:
            # Map entries are IPv4 by convention; return loopback v6 for the TLD.
            return RCODE.NOERROR, self.catch_all_ipv6
        # Other RR types: NOERROR with empty answer (covers SOA / NS probes).
        return RCODE.NOERROR, None

    def handle(self, data: bytes) -> bytes:
        try:
            request = DNSRecord.parse(data)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"Malformed DNS query dropped: {e}")
            # Best-effort: build a FORMERR reply.
            try:
                hdr = DNSRecord.parse(data[:12])
                hdr.header.set_rcode(RCODE.FORMERR)
                return bytes(hdr.pack())
            except Exception:  # noqa: BLE001
                return b""
        reply = request.reply()
        if request.q is None:
            reply.header.rcode = RCODE.FORMERR
            return bytes(reply.pack())
        qname = str(request.q.qname)
        qtype = request.q.qtype
        rcode, ip = self._resolve(qname, qtype)
        if rcode != RCODE.NOERROR:
            reply.header.rcode = rcode
            return bytes(reply.pack())
        if ip is not None:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                self.logger.error(f"Invalid IP {ip!r} for {qname}")
                reply.header.rcode = RCODE.SERVFAIL
                return bytes(reply.pack())
            rdata: Any
            if isinstance(addr, ipaddress.IPv4Address) and qtype == QTYPE.A:
                rdata = A(str(addr))
                reply.add_answer(
                    RR(
                        rname=request.q.qname,
                        rtype=QTYPE.A,
                        rclass=CLASS.IN,
                        ttl=_DEFAULT_TTL,
                        rdata=rdata,
                    )
                )
            elif isinstance(addr, ipaddress.IPv6Address) and qtype == QTYPE.AAAA:
                rdata = AAAA(str(addr))
                reply.add_answer(
                    RR(
                        rname=request.q.qname,
                        rtype=QTYPE.AAAA,
                        rclass=CLASS.IN,
                        ttl=_DEFAULT_TTL,
                        rdata=rdata,
                    )
                )
        return bytes(reply.pack())


class _Protocol(asyncio.DatagramProtocol):
    """Glue between asyncio's UDP transport and our DnsServer.handle()."""

    def __init__(self, server: DnsServer) -> None:
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # type: ignore[override]
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        reply = self.server.handle(data)
        if reply and self.transport is not None:
            self.transport.sendto(reply, addr)
