"""mkcert wrapper for local CA + per-project wildcard certificates.

mkcert is a hard external dependency. We do NOT reinvent CA / trust-store
integration in pure Python — mkcert handles macOS Keychain, Linux NSS, and
Windows cert store correctly across versions.

Usage:
  ensureCAInstalled() — call once at daemon startup; runs `mkcert -install`.
  mintCert(project)   — returns (cert_path, key_path) for *.{project}.localhost.
  buildCombinedBundle(caPath) — concat system CAs + mkcert root for env exports.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path

from . import paths as paths_mod
from .errors import ErrorCode, TaskmuxError
from .paths import ensureProjectCertDir, ensureTaskmuxDir, projectCertDir


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


def caRootPath() -> Path:
    """Resolve mkcert's root CA file (rootCA.pem) under `mkcert -CAROOT`.

    Raises TaskmuxError if rootCA.pem is missing — usually means the user has
    not run `taskmux ca install` yet.
    """
    bin_path = _mkcertBin()
    result = subprocess.run(
        [bin_path, "-CAROOT"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "(no output)"
        raise TaskmuxError(
            ErrorCode.INTERNAL,
            detail=f"mkcert -CAROOT failed: {msg}",
        )
    pem = Path(result.stdout.strip()) / "rootCA.pem"
    if not pem.exists():
        raise TaskmuxError(
            ErrorCode.INTERNAL,
            detail=f"rootCA.pem not found at {pem} — run 'taskmux ca install' first.",
        )
    return pem


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


# Hardcoded distro paths checked BEFORE ssl.get_default_verify_paths() because
# the latter honors SSL_CERT_FILE / SSL_CERT_DIR — which may already be pointing
# at mkcert's single-CA PEM from a prior broken trust-clients run, poisoning
# detection. Hardcoded paths are guaranteed Mozilla bundles.
_SYSTEM_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine, *BSD
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/CentOS/Fedora
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # newer RHEL
    "/etc/ca-certificates/extracted/tls-ca-bundle.pem",  # Arch
)


def systemCaBundle(exclude: Path | None = None) -> Path | None:
    """Locate the host's system CA bundle (Mozilla roots).

    Tries known distro locations first, then openssl's compiled-in default.
    Skips any candidate equal to `exclude` (typically mkcert's rootCA.pem) so
    a poisoned SSL_CERT_FILE pointing at mkcert can't masquerade as the system
    bundle. Returns None if nothing usable is found.
    """
    excludeReal: str | None = None
    if exclude is not None:
        with contextlib.suppress(OSError):
            excludeReal = os.path.realpath(exclude)

    def _ok(p: str) -> bool:
        if not os.path.isfile(p):
            return False
        if excludeReal is None:
            return True
        try:
            return os.path.realpath(p) != excludeReal
        except OSError:
            return True

    for p in _SYSTEM_CA_CANDIDATES:
        if _ok(p):
            return Path(p)
    cafile = ssl.get_default_verify_paths().cafile
    if cafile and _ok(cafile):
        return Path(cafile)
    return None


def combinedBundlePath() -> Path:
    """Stable path for the combined system+mkcert CA bundle."""
    return paths_mod.TASKMUX_DIR / "ca-bundle.pem"


def buildCombinedBundle(caPath: Path) -> Path:
    """Concatenate system CA bundle + mkcert root into ~/.taskmux/ca-bundle.pem.

    Why: pointing NODE_EXTRA_CA_CERTS / SSL_CERT_FILE at mkcert's single-CA
    rootCA.pem strands openssl-using tools (curl, npm, bun publish) — they
    lose access to public CAs and every TLS connection to a real registry
    fails with UNABLE_TO_GET_ISSUER_CERT_LOCALLY. The combined bundle keeps
    both trust paths working.

    Atomic-ish replace; idempotent. Raises if no system CA bundle locatable.
    """
    sys_ca = systemCaBundle(exclude=caPath)
    if sys_ca is None:
        raise TaskmuxError(
            ErrorCode.INTERNAL,
            detail=(
                "system CA bundle not found via ssl.get_default_verify_paths "
                "or known distro paths. Cannot build combined trust bundle "
                "— pointing env vars at mkcert root alone would break public "
                "TLS. Set SSL_CERT_FILE in your shell to your system bundle, "
                "then retry."
            ),
        )

    ensureTaskmuxDir()
    out = combinedBundlePath()
    sys_pem = sys_ca.read_text(encoding="utf-8").rstrip()
    mkcert_pem = caPath.read_text(encoding="utf-8").rstrip()
    blob = (
        f"# taskmux combined CA bundle — system roots + mkcert local CA\n"
        f"# system: {sys_ca}\n"
        f"# mkcert: {caPath}\n"
        f"# Regenerate with: taskmux ca trust-clients\n"
        f"{sys_pem}\n{mkcert_pem}\n"
    )

    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return out


def dropCert(project: str) -> None:
    """Remove cached cert files for a project (called on unregister)."""
    cert_dir = projectCertDir(project)
    if not cert_dir.exists():
        return
    for f in cert_dir.iterdir():
        with contextlib.suppress(OSError):
            f.unlink()
    with contextlib.suppress(OSError):
        cert_dir.rmdir()
