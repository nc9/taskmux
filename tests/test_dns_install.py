"""Tests for platform-specific DNS delegation install."""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import pytest

from taskmux import dns_install


def test_macos_install_writes_resolver_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_sys, "platform", "darwin")
    target = tmp_path / "resolver" / "mytld"
    monkeypatch.setattr(dns_install, "_macosResolverPath", lambda tld: target)

    dns_install.installDelegation("mytld", 5353)
    assert target.exists()
    assert target.read_text() == "nameserver 127.0.0.1\nport 5353\n"


def test_macos_install_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_sys, "platform", "darwin")
    target = tmp_path / "resolver" / "mytld"
    monkeypatch.setattr(dns_install, "_macosResolverPath", lambda tld: target)

    dns_install.installDelegation("mytld", 5353)
    mtime = target.stat().st_mtime_ns
    dns_install.installDelegation("mytld", 5353)
    # Same content → no rewrite, mtime unchanged.
    assert target.stat().st_mtime_ns == mtime


def test_macos_install_changes_port(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_sys, "platform", "darwin")
    target = tmp_path / "resolver" / "mytld"
    monkeypatch.setattr(dns_install, "_macosResolverPath", lambda tld: target)

    dns_install.installDelegation("mytld", 5353)
    dns_install.installDelegation("mytld", 9999)
    assert target.read_text() == "nameserver 127.0.0.1\nport 9999\n"


def test_macos_uninstall_removes_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_sys, "platform", "darwin")
    target = tmp_path / "resolver" / "mytld"
    monkeypatch.setattr(dns_install, "_macosResolverPath", lambda tld: target)

    dns_install.installDelegation("mytld", 5353)
    assert target.exists()
    dns_install.uninstallDelegation("mytld")
    assert not target.exists()


def test_linux_skips_when_no_resolvectl(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setattr(dns_install.shutil, "which", lambda _: None)
    monkeypatch.setattr(dns_install, "_LINUX_DROP_IN", tmp_path / "taskmux.conf")
    # Should not raise; should NOT write the drop-in.
    dns_install.installDelegation("mytld", 5353)
    assert not (tmp_path / "taskmux.conf").exists()


def test_linux_writes_drop_in_when_resolvectl_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setattr(dns_install.shutil, "which", lambda name: f"/usr/bin/{name}")
    target = tmp_path / "taskmux.conf"
    monkeypatch.setattr(dns_install, "_LINUX_DROP_IN", target)

    runs: list[list[str]] = []
    monkeypatch.setattr(
        dns_install.subprocess,
        "run",
        lambda cmd, **_kw: runs.append(cmd)
        or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    dns_install.installDelegation("mytld", 5353)
    assert target.exists()
    assert "DNS=127.0.0.1:5353" in target.read_text()
    assert "Domains=~mytld" in target.read_text()
    # systemctl reload should have been invoked.
    assert any("systemd-resolved" in c for c in runs[0])


def test_windows_install_raises_for_non_53_port(monkeypatch):
    monkeypatch.setattr(_sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="custom port"):
        dns_install.installDelegation("mytld", 5353)


def test_windows_install_raises_when_powershell_fails(monkeypatch):
    monkeypatch.setattr(_sys, "platform", "win32")

    def fake_run(cmd, **_kw):  # noqa: ANN001
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "Access denied"})()

    monkeypatch.setattr(dns_install.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Access denied"):
        dns_install.installDelegation("mytld", 53)


def test_windows_install_calls_powershell_for_53(monkeypatch):
    monkeypatch.setattr(_sys, "platform", "win32")
    runs: list[list[str]] = []
    monkeypatch.setattr(
        dns_install.subprocess,
        "run",
        lambda cmd, **_kw: runs.append(cmd)
        or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    dns_install.installDelegation("mytld", 53)
    assert len(runs) == 1
    assert runs[0][0] == "powershell"
    assert "Add-DnsClientNrptRule" in runs[0][-1]
    assert ".mytld" in runs[0][-1]


def test_unsupported_platform_raises(monkeypatch):
    monkeypatch.setattr(_sys, "platform", "freebsd13")
    with pytest.raises(RuntimeError, match="not supported"):
        dns_install.installDelegation("mytld", 5353)
