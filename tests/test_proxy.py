"""Tests for the reverse-proxy host parsing + route table.

Full end-to-end TLS tests need real certs (mkcert) and root binding (:443) — those
live in the integration script. These tests cover the units we own.
"""

from __future__ import annotations

import pytest

from taskmux.proxy import ProxyServer, _parseHost


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("api.alpha.localhost", ("alpha", "api")),
        ("api.alpha.localhost:443", ("alpha", "api")),
        ("API.ALPHA.LOCALHOST", ("alpha", "api")),
        ("web-1.beta.localhost", ("beta", "web-1")),
        ("a.b.c.proj.localhost", ("proj", "a.b.c")),
        # Apex: bare project hostname → empty host string.
        ("alpha.localhost", ("alpha", "")),
        ("alpha.localhost:443", ("alpha", "")),
        # Wildcard subdomain queries parse normally; fallback is a separate layer.
        ("anything.alpha.localhost", ("alpha", "anything")),
    ],
)
def test_parse_host_valid(host, expected):
    assert _parseHost(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "localhost",
        "localhost:443",
        ".localhost",
        "api.alpha.example.com",
        "127.0.0.1",
    ],
)
def test_parse_host_invalid(host):
    assert _parseHost(host) is None


def test_route_table_basic():
    proxy = ProxyServer()
    proxy.set_route("alpha", "api", 4001)
    proxy.set_route("alpha", "web", 4002)
    proxy.set_route("beta", "api", 5001)

    snap = proxy.routes_snapshot()
    assert snap["alpha"]["api"] == 4001
    assert snap["alpha"]["web"] == 4002
    assert snap["beta"]["api"] == 5001


def test_drop_route_removes_only_one():
    proxy = ProxyServer()
    proxy.set_route("alpha", "api", 4001)
    proxy.set_route("alpha", "web", 4002)
    proxy.drop_route("alpha", "api")

    snap = proxy.routes_snapshot()
    assert "api" not in snap.get("alpha", {})
    assert snap["alpha"]["web"] == 4002


def test_unregister_project_drops_routes_and_cert(tmp_path):
    proxy = ProxyServer()
    # Skip cert load — exercise route cleanup path only.
    proxy._routes[("alpha", "api")] = 4001
    proxy._routes[("alpha", "web")] = 4002
    proxy._routes[("beta", "api")] = 5001

    proxy.unregister_project("alpha")

    assert ("alpha", "api") not in proxy._routes
    assert ("alpha", "web") not in proxy._routes
    assert proxy._routes[("beta", "api")] == 5001


# ---------------------------------------------------------------------------
# Apex + wildcard route lookup precedence
# ---------------------------------------------------------------------------


def _route_lookup(proxy: ProxyServer, project: str, host: str) -> int | None:
    """Replicate the lookup precedence in `_handle` so we can assert it
    without spinning up an aiohttp test server."""
    port = proxy._routes.get((project, host))
    if port is None and host != "":
        port = proxy._routes.get((project, "*"))
    return port


def test_route_lookup_specific_wins_over_wildcard():
    proxy = ProxyServer()
    proxy.set_route("p", "api", 4001)
    proxy.set_route("p", "*", 9000)
    assert _route_lookup(proxy, "p", "api") == 4001


def test_route_lookup_wildcard_fallback():
    proxy = ProxyServer()
    proxy.set_route("p", "*", 9000)
    assert _route_lookup(proxy, "p", "tenant1") == 9000
    assert _route_lookup(proxy, "p", "anything-else") == 9000


def test_route_lookup_apex_no_wildcard_fallback():
    """An apex query (host == '') must NOT fall through to the wildcard
    route — apex and wildcard are independent route slots."""
    proxy = ProxyServer()
    proxy.set_route("p", "*", 9000)
    assert _route_lookup(proxy, "p", "") is None


def test_route_lookup_apex_hits_apex_route():
    proxy = ProxyServer()
    proxy.set_route("p", "", 8000)
    proxy.set_route("p", "*", 9000)
    assert _route_lookup(proxy, "p", "") == 8000
    assert _route_lookup(proxy, "p", "tenant") == 9000  # wildcard for non-apex


def test_route_lookup_no_match_returns_none():
    proxy = ProxyServer()
    proxy.set_route("p", "api", 4001)
    assert _route_lookup(proxy, "p", "unknown") is None
    assert _route_lookup(proxy, "p", "") is None
    assert _route_lookup(proxy, "other", "api") is None
