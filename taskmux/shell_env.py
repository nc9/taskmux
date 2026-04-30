"""Write env-var exports into a shell rc file so Node.js / Python trust the
mkcert root CA without any per-app config.

Why: `mkcert -install` only seeds the OS keychain. Node and Python read their
own CA bundles. Setting NODE_EXTRA_CA_CERTS / REQUESTS_CA_BUNDLE / SSL_CERT_FILE
to mkcert's rootCA.pem closes the gap.

Module is pure path/text manipulation — no subprocess, no mkcert. Caller passes
the resolved CA path in.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import stat
import tempfile
from pathlib import Path

from .errors import ErrorCode, TaskmuxError

_BEGIN = "# >>> taskmux trust-clients >>>"
_END = "# <<< taskmux trust-clients <<<"
_NOTE = "# Managed by `taskmux ca trust-clients`. Edits inside this block are overwritten."

_VARS = ("NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")

_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r".*?" + re.escape(_END),
    re.DOTALL,
)

_SUPPORTED = ("zsh", "bash", "fish")

_CLIENT_VARS = ("NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")


def clientTrustMissing() -> bool:
    """True if none of the client CA env vars are set in the current process.

    Heuristic for "user has not run trust-clients yet." Cheap — just checks
    the inherited env. False positives only happen if the user set one but
    not the others, which is unusual.
    """
    return not any(os.environ.get(name) for name in _CLIENT_VARS)


def detectShell(override: str | None = None) -> str:
    if override:
        sh = override
    else:
        raw = os.environ.get("SHELL", "")
        sh = Path(raw).name if raw else ""
    if sh not in _SUPPORTED:
        raise TaskmuxError(
            ErrorCode.INVALID_ARGUMENT,
            detail=(
                f"unsupported shell {sh!r}; pass --shell zsh|bash|fish "
                f"(detected from $SHELL={os.environ.get('SHELL', '')!r})"
            ),
        )
    return sh


def rcPathFor(shell: str) -> Path:
    home = Path.home()
    if shell == "zsh":
        return home / ".zshenv"
    if shell == "bash":
        return home / ".bashrc"
    if shell == "fish":
        return home / ".config" / "fish" / "config.fish"
    raise TaskmuxError(ErrorCode.INVALID_ARGUMENT, detail=f"unsupported shell {shell!r}")


def _fishQuote(s: str) -> str:
    """Single-quote for fish; backslash-escape `\\` and `'` only."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _exportLines(caPath: Path, shell: str) -> list[str]:
    p = str(caPath)
    if shell == "fish":
        q = _fishQuote(p)
        return [f"set -gx {name} {q}" for name in _VARS]
    q = shlex.quote(p)
    return [f"export {name}={q}" for name in _VARS]


def renderExportsOnly(caPath: Path, shell: str) -> str:
    return "\n".join(_exportLines(caPath, shell)) + "\n"


def renderBlock(caPath: Path, shell: str) -> str:
    lines = [_BEGIN, _NOTE, *_exportLines(caPath, shell), _END]
    return "\n".join(lines)


def _detectNewline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _atomicWrite(target: Path, content: str) -> None:
    """Write content to target atomically. If target is a symlink, follow it
    so the symlink itself stays intact.
    """
    final = target.resolve() if target.is_symlink() else target
    final.parent.mkdir(parents=True, exist_ok=True)
    existingMode: int | None = None
    if final.exists():
        existingMode = stat.S_IMODE(final.stat().st_mode)
    fd, tmp = tempfile.mkstemp(
        dir=str(final.parent),
        prefix=final.name + ".",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.chmod(tmp, existingMode if existingMode is not None else 0o644)
        os.replace(tmp, final)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def applyTrustClients(
    caPath: Path,
    shell: str,
    *,
    rcOverride: Path | None = None,
) -> dict:
    if os.name == "nt":
        return {
            "ok": False,
            "error": (
                "trust-clients is POSIX-only; on Windows mkcert -install "
                "configures Node via the system store"
            ),
        }

    rcPath = rcOverride if rcOverride is not None else rcPathFor(shell)
    block = renderBlock(caPath, shell)

    if rcPath.exists():
        with open(rcPath, encoding="utf-8", newline="") as f:
            existing = f.read()
    else:
        existing = ""

    nl = _detectNewline(existing) if existing else "\n"
    blockOut = block.replace("\n", nl)

    matches = list(_BLOCK_RE.finditer(existing))

    if not matches:
        if existing and not existing.endswith(nl):
            new = existing + nl + nl + blockOut + nl
        elif existing:
            sep = "" if existing.endswith(nl + nl) else nl
            new = existing + sep + blockOut + nl
        else:
            new = blockOut + nl
        action = "wrote"
    elif len(matches) == 1:
        m = matches[0]
        currentBlock = existing[m.start() : m.end()]
        if currentBlock == blockOut:
            return {
                "ok": True,
                "action": "unchanged",
                "rcFile": str(rcPath),
                "caPath": str(caPath),
                "sourceCmd": f"source {rcPath}",
            }
        new = existing[: m.start()] + blockOut + existing[m.end() :]
        action = "replaced"
    else:
        first = matches[0]
        head = existing[: first.start()]
        tail = existing[first.end() :]
        tail = _BLOCK_RE.sub("", tail)
        new = head + blockOut + tail
        action = "replaced"

    _atomicWrite(rcPath, new)
    return {
        "ok": True,
        "action": action,
        "rcFile": str(rcPath),
        "caPath": str(caPath),
        "sourceCmd": f"source {rcPath}",
    }
