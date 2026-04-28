"""mkcert wrapper for local CA + per-project wildcard certificates.

mkcert is a hard external dependency. We do NOT reinvent CA / trust-store
integration in pure Python — mkcert handles macOS Keychain, Linux NSS, and
Windows cert store correctly across versions.

Usage:
  ensureCAInstalled() — call once at daemon startup; runs `mkcert -install`.
  mintCert(project)   — returns (cert_path, key_path) for *.{project}.localhost.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import ErrorCode, TaskmuxError
from .paths import ensureProjectCertDir, projectCertDir


class MkcertMissing(TaskmuxError):
    """mkcert binary not found on PATH."""

    def __init__(self) -> None:
        super().__init__(
            ErrorCode.INTERNAL,
            detail=(
                "mkcert not found on PATH. Install with `brew install mkcert nss` "
                "(macOS) or see https://github.com/FiloSottile/mkcert#installation."
            ),
        )


def _mkcertBin() -> str:
    bin_path = shutil.which("mkcert")
    if not bin_path:
        raise MkcertMissing()
    return bin_path


def ensureCAInstalled() -> None:
    """Idempotent: install mkcert's local CA into the system trust store.

    Raises TaskmuxError if mkcert exits nonzero — the user almost certainly
    cancelled a keychain/sudo prompt and the CA is not trusted.
    """
    bin_path = _mkcertBin()
    result = subprocess.run(
        [bin_path, "-install"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise TaskmuxError(
            ErrorCode.INTERNAL,
            detail=f"mkcert -install failed: {msg}",
        )


def mintCert(project: str) -> tuple[Path, Path]:
    """Mint cert + key for *.{project}.localhost. Cached at ~/.taskmux/certs/{project}/."""
    bin_path = _mkcertBin()
    cert_dir = ensureProjectCertDir(project)
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"

    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    wildcard = f"*.{project}.localhost"
    bare = f"{project}.localhost"
    result = subprocess.run(
        [
            bin_path,
            "-cert-file",
            str(cert_path),
            "-key-file",
            str(key_path),
            wildcard,
            bare,
            "localhost",
            "127.0.0.1",
            "::1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        raise TaskmuxError(
            ErrorCode.INTERNAL,
            detail=f"mkcert failed for {project!r}: {msg}",
        )
    return cert_path, key_path


def dropCert(project: str) -> None:
    """Remove cached cert files for a project (called on unregister)."""
    import contextlib

    cert_dir = projectCertDir(project)
    if not cert_dir.exists():
        return
    for f in cert_dir.iterdir():
        with contextlib.suppress(OSError):
            f.unlink()
    with contextlib.suppress(OSError):
        cert_dir.rmdir()
