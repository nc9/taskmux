"""Tests for mkcert wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from taskmux import ca
from taskmux import paths as paths_mod
from taskmux.errors import ErrorCode


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "CERTS_DIR", tmp_path / "certs")
    return tmp_path


def test_missing_mkcert_raises_clear_error(isolated_paths, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: None)
    with pytest.raises(ca.MkcertMissing) as exc_info:
        ca.ensureCAInstalled()
    assert "mkcert not found" in exc_info.value.message
    assert exc_info.value.code == ErrorCode.INTERNAL


def test_ensure_ca_installed_calls_mkcert_install(isolated_paths, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        calls.append(cmd)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    ca.ensureCAInstalled()
    assert calls == [["/usr/bin/mkcert", "-install"]]


def test_ensure_ca_installed_raises_on_nonzero(isolated_paths, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        class _R:
            returncode = 1
            stdout = ""
            stderr = "user denied keychain prompt"

        return _R()

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    with pytest.raises(Exception) as exc_info:
        ca.ensureCAInstalled()
    assert "user denied keychain prompt" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.INTERNAL


def test_mint_cert_writes_files_and_returns_paths(isolated_paths, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")
    captured: dict = {}

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        captured["cmd"] = cmd
        cert_idx = cmd.index("-cert-file") + 1
        key_idx = cmd.index("-key-file") + 1
        Path(cmd[cert_idx]).write_text("FAKECERT")
        Path(cmd[key_idx]).write_text("FAKEKEY")

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    cert, key = ca.mintCert("alpha")
    assert cert.exists()
    assert key.exists()
    assert cert.read_text() == "FAKECERT"
    assert "*.alpha.localhost" in captured["cmd"]
    assert "alpha.localhost" in captured["cmd"]


def test_mint_cert_idempotent_when_cached(isolated_paths, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")
    cert_dir = paths_mod.CERTS_DIR / "alpha"
    cert_dir.mkdir(parents=True)
    (cert_dir / "cert.pem").write_text("EXISTING")
    (cert_dir / "key.pem").write_text("EXISTING")

    with patch.object(ca.subprocess, "run") as mock_run:
        ca.mintCert("alpha")
        mock_run.assert_not_called()


def test_ca_root_path_returns_pem(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")
    caroot = tmp_path / "caroot"
    caroot.mkdir()
    (caroot / "rootCA.pem").write_text("FAKEROOT")

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        class _R:
            returncode = 0
            stdout = f"{caroot}\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    pem = ca.caRootPath()
    assert pem == caroot / "rootCA.pem"
    assert pem.read_text() == "FAKEROOT"


def test_ca_root_path_raises_when_pem_missing(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")
    caroot = tmp_path / "empty-caroot"
    caroot.mkdir()

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        class _R:
            returncode = 0
            stdout = f"{caroot}\n"
            stderr = ""

        return _R()

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    with pytest.raises(Exception) as exc_info:
        ca.caRootPath()
    assert "rootCA.pem not found" in str(exc_info.value)
    assert "taskmux ca install" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.INTERNAL


def test_mint_cert_failure_raises(isolated_paths, monkeypatch):
    monkeypatch.setattr(ca.shutil, "which", lambda _name: "/usr/bin/mkcert")

    def fake_run(cmd, **_kwargs):  # noqa: ANN001
        class _R:
            returncode = 1
            stdout = ""
            stderr = "boom"

        return _R()

    monkeypatch.setattr(ca.subprocess, "run", fake_run)
    with pytest.raises(Exception) as exc_info:
        ca.mintCert("alpha")
    assert "boom" in str(exc_info.value)


def test_build_combined_bundle_concats_system_and_mkcert(isolated_paths, monkeypatch, tmp_path):
    sys_ca = tmp_path / "system.pem"
    sys_ca.write_text("-----BEGIN CERTIFICATE-----\nSYSROOT\n-----END CERTIFICATE-----\n")
    mkcert_pem = tmp_path / "rootCA.pem"
    mkcert_pem.write_text("-----BEGIN CERTIFICATE-----\nMKCERT\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(ca, "systemCaBundle", lambda exclude=None: sys_ca)

    out = ca.buildCombinedBundle(mkcert_pem)

    assert out == ca.combinedBundlePath()
    body = out.read_text()
    assert "SYSROOT" in body
    assert "MKCERT" in body
    assert body.index("SYSROOT") < body.index("MKCERT")


def test_build_combined_bundle_idempotent(isolated_paths, monkeypatch, tmp_path):
    sys_ca = tmp_path / "system.pem"
    sys_ca.write_text("SYS\n")
    mkcert_pem = tmp_path / "rootCA.pem"
    mkcert_pem.write_text("MK\n")
    monkeypatch.setattr(ca, "systemCaBundle", lambda exclude=None: sys_ca)

    out1 = ca.buildCombinedBundle(mkcert_pem)
    body1 = out1.read_text()
    out2 = ca.buildCombinedBundle(mkcert_pem)
    body2 = out2.read_text()

    assert out1 == out2
    assert body1 == body2


def test_build_combined_bundle_replaces_stale(isolated_paths, monkeypatch, tmp_path):
    sys_ca = tmp_path / "system.pem"
    sys_ca.write_text("OLD-SYS\n")
    mkcert_pem = tmp_path / "rootCA.pem"
    mkcert_pem.write_text("MK\n")
    monkeypatch.setattr(ca, "systemCaBundle", lambda exclude=None: sys_ca)
    ca.buildCombinedBundle(mkcert_pem)
    sys_ca.write_text("NEW-SYS\n")

    out = ca.buildCombinedBundle(mkcert_pem)
    body = out.read_text()
    assert "NEW-SYS" in body
    assert "OLD-SYS" not in body


def test_build_combined_bundle_raises_when_no_system_ca(isolated_paths, monkeypatch, tmp_path):
    monkeypatch.setattr(ca, "systemCaBundle", lambda exclude=None: None)
    mkcert_pem = tmp_path / "rootCA.pem"
    mkcert_pem.write_text("MK\n")
    with pytest.raises(Exception) as exc_info:
        ca.buildCombinedBundle(mkcert_pem)
    assert "system CA bundle not found" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.INTERNAL


def test_system_ca_bundle_prefers_candidate_over_ssl_default(monkeypatch, tmp_path):
    """Candidate paths beat ssl.get_default_verify_paths so a poisoned
    SSL_CERT_FILE can't masquerade as the system bundle."""
    candidate = tmp_path / "candidate.pem"
    candidate.write_text("X")
    poisoned = tmp_path / "mkcert-poisoned.pem"
    poisoned.write_text("Y")

    class _Paths:
        cafile = str(poisoned)

    monkeypatch.setattr(ca.ssl, "get_default_verify_paths", lambda: _Paths())
    monkeypatch.setattr(ca, "_SYSTEM_CA_CANDIDATES", (str(candidate),))
    assert ca.systemCaBundle() == candidate


def test_system_ca_bundle_falls_back_to_ssl_default(monkeypatch, tmp_path):
    real = tmp_path / "default.pem"
    real.write_text("X")

    class _Paths:
        cafile = str(real)

    monkeypatch.setattr(ca.ssl, "get_default_verify_paths", lambda: _Paths())
    monkeypatch.setattr(ca, "_SYSTEM_CA_CANDIDATES", ("/nonexistent/x.pem",))
    assert ca.systemCaBundle() == real


def test_system_ca_bundle_excludes_mkcert_path(monkeypatch, tmp_path):
    """Even if a candidate path resolves to mkcert's rootCA.pem (e.g. user
    symlinked /etc/ssl/cert.pem), excluding it must skip to the next option."""
    mkcert_real = tmp_path / "rootCA.pem"
    mkcert_real.write_text("MK")
    poisoned_link = tmp_path / "etc-ssl-cert.pem"
    poisoned_link.symlink_to(mkcert_real)
    fallback = tmp_path / "real-system.pem"
    fallback.write_text("SYS")

    class _Paths:
        cafile = str(fallback)

    monkeypatch.setattr(ca.ssl, "get_default_verify_paths", lambda: _Paths())
    monkeypatch.setattr(ca, "_SYSTEM_CA_CANDIDATES", (str(poisoned_link),))
    assert ca.systemCaBundle(exclude=mkcert_real) == fallback


def test_system_ca_bundle_returns_none_when_nothing_present(monkeypatch):
    class _Paths:
        cafile = None

    monkeypatch.setattr(ca.ssl, "get_default_verify_paths", lambda: _Paths())
    monkeypatch.setattr(ca, "_SYSTEM_CA_CANDIDATES", ("/nonexistent/x.pem",))
    assert ca.systemCaBundle() is None
