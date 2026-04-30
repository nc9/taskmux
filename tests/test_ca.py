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
