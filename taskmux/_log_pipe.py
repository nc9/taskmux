"""Stdin-to-file timestamper with rotation, invoked by tmux pipe-pane.

Usage: python3 -u -m taskmux._log_pipe <log_path> <max_bytes> <max_files>
"""

from __future__ import annotations

import contextlib
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path


def rotateLogs(log_path: Path, max_files: int) -> None:
    """Rotate log files: task.log → task.log.1 → task.log.2 etc."""
    # Delete oldest if at limit
    oldest = Path(f"{log_path}.{max_files}")
    if oldest.exists():
        oldest.unlink()

    # Shift existing rotated files
    for i in range(max_files - 1, 0, -1):
        src = Path(f"{log_path}.{i}")
        dst = Path(f"{log_path}.{i + 1}")
        if src.exists():
            src.rename(dst)

    # Rotate current to .1
    if log_path.exists():
        log_path.rename(Path(f"{log_path}.1"))


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(1)

    log_path = Path(sys.argv[1])
    max_bytes = int(sys.argv[2])
    max_files = int(sys.argv[3])

    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Ignore SIGPIPE — handle BrokenPipeError instead
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    f = open(log_path, "a")  # noqa: SIM115
    try:
        for raw_line in sys.stdin.buffer:
            try:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
            except Exception:
                continue

            now = datetime.now(UTC)
            ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
            f.write(f"{ts} {line}\n")
            f.flush()

            # Check rotation
            try:
                if f.tell() >= max_bytes:
                    f.close()
                    rotateLogs(log_path, max_files)
                    f = open(log_path, "a")  # noqa: SIM115
            except OSError:
                pass
    except (BrokenPipeError, OSError, KeyboardInterrupt):
        pass
    finally:
        with contextlib.suppress(Exception):
            f.close()


if __name__ == "__main__":
    main()
