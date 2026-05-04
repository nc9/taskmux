"""Event recording and querying for taskmux lifecycle events."""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path

from .event_bus import publishEventSync

EVENTS_DIR = Path.home() / ".taskmux"
EVENTS_FILE = EVENTS_DIR / "events.jsonl"
MAX_LINES = 15_000
TRIM_TO = 10_000


def recordEvent(
    event: str,
    session: str,
    task: str | None = None,
    **extra: str | int | bool | list[str] | None,
) -> dict:
    """Append event to events.jsonl AND publish to the in-process event bus.

    The bus push is best-effort: when no event loop is running (CLI one-shot
    invocations) it's a no-op. JSONL write is the durable path.
    """
    EVENTS_DIR.mkdir(exist_ok=True)

    entry: dict = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "session": session,
    }
    if task:
        entry["task"] = task
    entry.update({k: v for k, v in extra.items() if v is not None})

    line = json.dumps(entry, default=str) + "\n"

    with open(EVENTS_FILE, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        fcntl.flock(f, fcntl.LOCK_UN)

    _maybeRotate()
    publishEventSync(entry)
    return entry


def _maybeRotate() -> None:
    """Trim file if it exceeds MAX_LINES."""
    if not EVENTS_FILE.exists():
        return
    lines = EVENTS_FILE.read_text().splitlines()
    if len(lines) > MAX_LINES:
        EVENTS_FILE.write_text("\n".join(lines[-TRIM_TO:]) + "\n")


def queryEvents(
    task: str | None = None,
    session: str | None = None,
    project: str | None = None,
    worktree: str | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read and filter events from the JSONL file."""
    if not EVENTS_FILE.exists():
        return []

    results: list[dict] = []
    for line in EVENTS_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if task and entry.get("task") != task:
            continue
        if session and entry.get("session") != session:
            continue
        if project and entry.get("project") != project:
            continue
        if worktree and entry.get("worktree") != worktree:
            continue
        if since:
            try:
                entry_ts = datetime.fromisoformat(entry["ts"])
                if entry_ts < since:
                    continue
            except (ValueError, KeyError):
                continue
        results.append(entry)

    return results[-limit:]
