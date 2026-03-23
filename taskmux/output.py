"""JSON output mode for programmatic CLI consumption."""

from __future__ import annotations

import json
import sys
from contextvars import ContextVar

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
