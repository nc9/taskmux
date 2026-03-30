"""JSON output mode for programmatic CLI consumption."""

from __future__ import annotations

import json
import sys
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .errors import TaskmuxError

_json_mode: ContextVar[bool] = ContextVar("json_mode", default=False)


def is_json_mode() -> bool:
    return _json_mode.get()


def set_json_mode(value: bool) -> None:
    _json_mode.set(value)


def print_result(data: dict | list) -> None:
    """Print JSON to stdout if json mode is active."""
    if is_json_mode():
        json.dump(data, sys.stdout, default=str)
        sys.stdout.write("\n")
        sys.stdout.flush()


def print_jsonl(data: dict) -> None:
    """Print a single JSONL line (for streaming)."""
    json.dump(data, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def print_error(err: TaskmuxError) -> None:
    """Render a TaskmuxError as JSON or human-readable Rich output."""
    if is_json_mode():
        json.dump(err.to_dict(), sys.stderr, default=str)
        sys.stderr.write("\n")
        sys.stderr.flush()
    else:
        from rich.console import Console

        console = Console(stderr=True)
        console.print(f"Error [{err.code}]: {err.message}", style="red")
