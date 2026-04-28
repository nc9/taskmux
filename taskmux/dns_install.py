"""Platform-specific DNS-delegation install for the in-process DNS server.

The DNS server itself is unprivileged (binds 127.0.0.1:5353). What needs
root is telling the OS to send `*.<tld>` queries to it. Each platform has
its own mechanism, kept here so the server logic stays clean.

  - macOS:   /etc/resolver/<tld> file (per-TLD delegation, supports custom port).
  - Linux:   systemd-resolved drop-in at /etc/systemd/resolved.conf.d/taskmux.conf
             (custom port supported in resolved 247+; older versions ignore it).
  - Windows: NRPT rule via PowerShell. NRPT does NOT support a custom port,
             so on Windows the DNS server must bind :53 (Admin once).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("taskmux-daemon.dns_install")

_LINUX_DROP_IN = Path("/etc/systemd/resolved.conf.d/taskmux.conf")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _macosResolverPath(tld: str) -> Path:
    return Path("/etc/resolver") / tld


def _atomicWrite(path: Path, text: str, mode: int = 0o644) -> None:
    """Same-directory tempfile + os.replace: atomic on POSIX and Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".taskmux-tmp")
    tmp.write_text(text)
    with contextlib.suppress(OSError):
        os.chmod(tmp, mode)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def installDelegation(tld: str, port: int) -> None:
    """Make the OS send `*.<tld>` queries to 127.0.0.1:<port>. Idempotent.

    Requires root/Admin. Raises on hard failure; logs and returns on
    "platform unsupported, fall back to etc_hosts".
    """
    plat = sys.platform
    if plat == "darwin":
        _installMacos(tld, port)
    elif plat.startswith("linux"):
        _installLinux(tld, port)
    elif plat.startswith("win"):
        _installWindows(tld, port)
    else:
        raise RuntimeError(f"DNS delegation not supported on platform {plat!r}")


def uninstallDelegation(tld: str) -> None:
    plat = sys.platform
    if plat == "darwin":
        _uninstallMacos(tld)
    elif plat.startswith("linux"):
        _uninstallLinux()
    elif plat.startswith("win"):
        _uninstallWindows(tld)


def flushDnsCache() -> None:
    """Best-effort DNS cache flush after delegation install/uninstall."""
    plat = sys.platform
    try:
        if plat == "darwin":
            subprocess.run(["dscacheutil", "-flushcache"], check=False)
            subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)
        elif plat.startswith("linux"):
            if shutil.which("resolvectl"):
                subprocess.run(["resolvectl", "flush-caches"], check=False)
        elif plat.startswith("win"):
            subprocess.run(["ipconfig", "/flushdns"], check=False)
    except OSError as e:
        logger.warning(f"DNS cache flush failed: {e}")


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def _installMacos(tld: str, port: int) -> None:
    path = _macosResolverPath(tld)
    body = f"nameserver 127.0.0.1\nport {port}\n"
    if path.exists() and path.read_text() == body:
        logger.info(f"macOS resolver already installed at {path}")
        return
    _atomicWrite(path, body)
    logger.info(f"Installed macOS resolver: {path} -> 127.0.0.1:{port}")


def _uninstallMacos(tld: str) -> None:
    path = _macosResolverPath(tld)
    if path.exists():
        with contextlib.suppress(OSError):
            path.unlink()
        logger.info(f"Removed macOS resolver: {path}")


# ---------------------------------------------------------------------------
# Linux (systemd-resolved)
# ---------------------------------------------------------------------------


def _installLinux(tld: str, port: int) -> None:
    if not shutil.which("resolvectl"):
        logger.warning(
            "Linux DNS delegation needs systemd-resolved (resolvectl not found). "
            'Set host_resolver = "etc_hosts" in ~/.taskmux/config.toml.'
        )
        return
    body = f"[Resolve]\nDNS=127.0.0.1:{port}\nDomains=~{tld}\n"
    if _LINUX_DROP_IN.exists() and _LINUX_DROP_IN.read_text() == body:
        logger.info(f"systemd-resolved drop-in already in place at {_LINUX_DROP_IN}")
    else:
        _atomicWrite(_LINUX_DROP_IN, body)
        logger.info(f"Installed systemd-resolved drop-in: {_LINUX_DROP_IN} (.{tld} -> :{port})")
    subprocess.run(["systemctl", "reload-or-restart", "systemd-resolved"], check=False)


def _uninstallLinux() -> None:
    if _LINUX_DROP_IN.exists():
        with contextlib.suppress(OSError):
            _LINUX_DROP_IN.unlink()
        logger.info(f"Removed systemd-resolved drop-in: {_LINUX_DROP_IN}")
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "reload-or-restart", "systemd-resolved"], check=False)


# ---------------------------------------------------------------------------
# Windows (NRPT)
# ---------------------------------------------------------------------------


def _installWindows(tld: str, port: int) -> None:
    if port != 53:
        raise RuntimeError(
            f"Windows NRPT can't redirect to a custom port (got {port}); "
            f"only :53 works. Either set dns_server_port = 53 in "
            f"~/.taskmux/config.toml (Admin required for the bind) or "
            f'use host_resolver = "etc_hosts".'
        )
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"Add-DnsClientNrptRule -Namespace '.{tld}' "
            f"-NameServers '127.0.0.1' -Comment 'taskmux' -Force"
        ),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise RuntimeError(f"NRPT install failed (rc={result.returncode}): {msg}")
    logger.info(f"Installed NRPT rule for .{tld} -> 127.0.0.1")


def _uninstallWindows(tld: str) -> None:
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"Get-DnsClientNrptRule | Where-Object {{ $_.Comment -eq 'taskmux' "
            f"-and $_.Namespace -contains '.{tld}' }} | "
            f"Remove-DnsClientNrptRule -Force"
        ),
    ]
    subprocess.run(cmd, check=False, capture_output=True, text=True)
    logger.info(f"Removed NRPT rule for .{tld}")
