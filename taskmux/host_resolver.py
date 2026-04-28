"""Pluggable hostname resolution for the proxy.

Users hit URLs like `https://api.oddjob.localhost/`. For the browser to reach
the proxy we need that name to resolve to 127.0.0.1. macOS doesn't resolve
`*.localhost` natively; Windows doesn't either; Linux usually does via
nss-myhostname but not always. So taskmux ships a pluggable resolver:

  - EtcHostsResolver — writes a managed block to the system hosts file.
    Default. Requires admin privileges. macOS / Linux / Windows.
  - NoopResolver    — does nothing. Use if you handle resolution yourself
    (a tunnel, custom DNS, /etc/resolver, dnsmasq, etc.).

Future implementations could add: DnsmasqResolver, CloudflareTunnelResolver,
NgrokResolver, ResolverFileResolver (macOS /etc/resolver/<TLD>), …

The abstraction is intentionally name-agnostic: callers pass a list of
`(fqdn, ip)` mappings, the resolver makes them resolvable. Nothing in this
module assumes `.localhost`, so swapping in a different domain later is just
a config change at the call site.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

HostMapping = tuple[str, str]  # (fqdn, ip)

_BEGIN_MARKER = "# BEGIN taskmux managed"
_END_MARKER = "# END taskmux managed"


def _systemHostsPath() -> Path:
    """Return the platform's system hosts file path."""
    if sys.platform.startswith("win"):
        # Windows env var keys are case-insensitive in practice; both work.
        sys_root = (
            os.environ.get("SYSTEMROOT")
            or os.environ.get("SystemRoot")  # noqa: SIM112
            or r"C:\Windows"
        )
        return Path(sys_root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


@runtime_checkable
class HostResolver(Protocol):
    """Make proxy hostnames reachable from this machine.

    Implementations are responsible for whatever side effect makes the
    given fqdns resolve to the given IP addresses (write hosts file,
    register a tunnel, update DDNS, etc.). Operations must be idempotent.
    """

    name: str

    def sync(self, mappings: list[HostMapping]) -> None:
        """Replace all taskmux-managed mappings with this set."""
        ...

    def clear(self) -> None:
        """Remove all taskmux-managed mappings."""
        ...


# ---------------------------------------------------------------------------
# /etc/hosts (and Windows %SystemRoot%\System32\drivers\etc\hosts) resolver
# ---------------------------------------------------------------------------


class EtcHostsResolver:
    """Maintain a `# BEGIN taskmux managed` / `# END taskmux managed` block
    in the system hosts file. Idempotent."""

    name = "etc_hosts"

    def __init__(self, hosts_file: Path | None = None) -> None:
        self.path = hosts_file or _systemHostsPath()
        self.logger = logging.getLogger("taskmux-daemon.host_resolver")

    def sync(self, mappings: list[HostMapping]) -> None:
        original = self.path.read_text() if self.path.exists() else ""
        outside = _strip_block(original)
        rendered = _render_block(mappings)
        new_text = outside.rstrip("\n") + ("\n\n" if outside.strip() else "") + rendered
        if new_text == original:
            return
        self._atomic_write(new_text)
        self.logger.info(f"Synced {len(mappings)} hostname(s) into {self.path} via {self.name}")

    def clear(self) -> None:
        if not self.path.exists():
            return
        text = self.path.read_text()
        stripped = _strip_block(text)
        if stripped != text:
            self._atomic_write(stripped.rstrip("\n") + "\n")
            self.logger.info(f"Cleared taskmux-managed block from {self.path}")

    def _atomic_write(self, text: str) -> None:
        # Same-directory tempfile + os.replace → atomic on POSIX and Windows.
        # Hosts file owner/mode is preserved on the existing file via tempfile
        # ownership inheriting from us (root); macOS keeps mode 0644 by default.
        tmp = self.path.with_suffix(self.path.suffix + ".taskmux-tmp")
        tmp.write_text(text)
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o644)
        os.replace(tmp, self.path)


def _strip_block(text: str) -> str:
    """Remove any existing taskmux-managed block from text."""
    if _BEGIN_MARKER not in text:
        return text
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _BEGIN_MARKER:
            in_block = True
            continue
        if stripped == _END_MARKER:
            in_block = False
            continue
        if not in_block:
            out.append(line)
    result = "\n".join(out)
    if text.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _render_block(mappings: list[HostMapping]) -> str:
    lines = [_BEGIN_MARKER]
    for fqdn, ip in sorted(mappings):
        lines.append(f"{ip}\t{fqdn}")
    lines.append(_END_MARKER)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# No-op resolver (when an external system handles resolution)
# ---------------------------------------------------------------------------


class NoopResolver:
    """Don't do anything. Use when resolution is handled externally —
    a tunnel, custom DNS, /etc/resolver/<TLD>, mDNS, etc."""

    name = "noop"

    def sync(self, mappings: list[HostMapping]) -> None:
        return

    def clear(self) -> None:
        return


# ---------------------------------------------------------------------------
# In-process DNS server resolver — dynamic, no privilege after delegation install
# ---------------------------------------------------------------------------


class DnsServerResolver:
    """Wrap a running DnsServer; sync() updates its in-memory map.

    The daemon constructs the DnsServer instance and passes it in. After the
    one-time delegation install (writes /etc/resolver/<tld> on macOS, etc.),
    every subsequent project/task host change is a pure in-memory update —
    no /etc/hosts rewrite, no privilege required, no daemon restart.
    """

    name = "dns_server"

    def __init__(self, server) -> None:  # type: ignore[no-untyped-def]
        self._server = server

    def sync(self, mappings: list[HostMapping]) -> None:
        self._server.update(mappings)

    def clear(self) -> None:
        self._server.update([])


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


# Resolvers that take no runtime context — instantiated by name alone.
_SIMPLE_RESOLVERS: dict[str, type] = {
    "etc_hosts": EtcHostsResolver,
    "noop": NoopResolver,
}


def availableResolvers() -> list[str]:
    return sorted([*_SIMPLE_RESOLVERS, "dns_server"])


def getResolver(name: str, *, dns_server=None) -> HostResolver:  # type: ignore[no-untyped-def]
    """Build a resolver by name. Pass `dns_server=` for `dns_server` impl."""
    if name == "dns_server":
        if dns_server is None:
            raise ValueError("host_resolver = 'dns_server' requires a running DnsServer instance")
        return DnsServerResolver(dns_server)
    cls = _SIMPLE_RESOLVERS.get(name)
    if cls is None:
        raise ValueError(f"unknown host_resolver {name!r}; available: {availableResolvers()}")
    return cls()
