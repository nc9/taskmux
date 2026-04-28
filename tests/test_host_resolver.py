"""Tests for the pluggable host resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskmux.host_resolver import (
    DnsServerResolver,
    EtcHostsResolver,
    NoopResolver,
    availableResolvers,
    getResolver,
)


def test_factory_returns_etc_hosts_by_name():
    r = getResolver("etc_hosts")
    assert isinstance(r, EtcHostsResolver)


def test_factory_returns_noop_by_name():
    r = getResolver("noop")
    assert isinstance(r, NoopResolver)


def test_factory_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown host_resolver"):
        getResolver("nonsense")


def test_available_resolvers_lists_built_ins():
    names = availableResolvers()
    assert "etc_hosts" in names
    assert "noop" in names


def test_etc_hosts_resolver_writes_managed_block(tmp_path: Path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n::1\tlocalhost\n")
    r = EtcHostsResolver(hosts_file=hosts)
    r.sync([("api.demo.localhost", "127.0.0.1"), ("web.demo.localhost", "127.0.0.1")])
    text = hosts.read_text()
    # User content preserved
    assert "127.0.0.1\tlocalhost" in text
    # Managed block present
    assert "# BEGIN taskmux managed" in text
    assert "# END taskmux managed" in text
    assert "127.0.0.1\tapi.demo.localhost" in text
    assert "127.0.0.1\tweb.demo.localhost" in text


def test_etc_hosts_resolver_replaces_block_idempotently(tmp_path: Path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n")
    r = EtcHostsResolver(hosts_file=hosts)
    r.sync([("a.demo.localhost", "127.0.0.1"), ("b.demo.localhost", "127.0.0.1")])
    first = hosts.read_text()
    # Second sync with same set should be a no-op (text unchanged).
    r.sync([("a.demo.localhost", "127.0.0.1"), ("b.demo.localhost", "127.0.0.1")])
    assert hosts.read_text() == first
    # Third sync with a different set replaces the block, doesn't accumulate.
    r.sync([("only.demo.localhost", "127.0.0.1")])
    text = hosts.read_text()
    assert "only.demo.localhost" in text
    assert "a.demo.localhost" not in text
    assert "b.demo.localhost" not in text
    # Only ONE managed block, not two
    assert text.count("# BEGIN taskmux managed") == 1


def test_etc_hosts_resolver_clear_removes_block(tmp_path: Path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n")
    r = EtcHostsResolver(hosts_file=hosts)
    r.sync([("api.demo.localhost", "127.0.0.1")])
    assert "# BEGIN taskmux managed" in hosts.read_text()
    r.clear()
    text = hosts.read_text()
    assert "# BEGIN taskmux managed" not in text
    assert "127.0.0.1\tlocalhost" in text


def test_etc_hosts_resolver_handles_missing_file(tmp_path: Path):
    hosts = tmp_path / "hosts"  # doesn't exist
    r = EtcHostsResolver(hosts_file=hosts)
    r.sync([("api.demo.localhost", "127.0.0.1")])
    assert hosts.exists()
    assert "api.demo.localhost" in hosts.read_text()


def test_noop_resolver_does_nothing(tmp_path: Path):
    r = NoopResolver()
    r.sync([("api.demo.localhost", "127.0.0.1")])
    r.clear()
    # No file or side effect; just checking it doesn't blow up.


def test_dns_server_resolver_sync_passes_through_to_server():
    class _FakeServer:
        def __init__(self):
            self.last: list = []

        def update(self, mappings):
            self.last = list(mappings)

    fake = _FakeServer()
    r = DnsServerResolver(fake)
    r.sync([("api.demo.localhost", "127.0.0.1")])
    assert fake.last == [("api.demo.localhost", "127.0.0.1")]
    r.clear()
    assert fake.last == []


def test_factory_dns_server_requires_runtime_context():
    with pytest.raises(ValueError, match="requires a running DnsServer"):
        getResolver("dns_server")


def test_factory_dns_server_with_runtime_context():
    class _FakeServer:
        def update(self, mappings):
            pass

    r = getResolver("dns_server", dns_server=_FakeServer())
    assert isinstance(r, DnsServerResolver)


def test_available_resolvers_lists_dns_server():
    assert "dns_server" in availableResolvers()


def test_etc_hosts_default_path_per_platform(monkeypatch):
    """Default path uses /etc/hosts on POSIX; %SystemRoot% on Windows."""
    import sys as _sys

    from taskmux.host_resolver import _systemHostsPath

    monkeypatch.setattr(_sys, "platform", "darwin")
    assert _systemHostsPath() == Path("/etc/hosts")

    monkeypatch.setattr(_sys, "platform", "linux")
    assert _systemHostsPath() == Path("/etc/hosts")

    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    assert _systemHostsPath() == Path(r"C:\Windows") / "System32" / "drivers" / "etc" / "hosts"
