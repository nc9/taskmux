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
        "alpha.localhost",
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
