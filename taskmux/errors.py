"""Centralized error codes, messages, and exception for Taskmux."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """All taskmux error codes."""

    # Config errors (E1xx)
    CONFIG_NOT_FOUND = "E100"
    CONFIG_PARSE_ERROR = "E101"
    CONFIG_INVALID_TASK = "E102"
    CONFIG_UNKNOWN_KEYS = "E103"
    CONFIG_VALIDATION = "E104"
    CONFIG_ALREADY_EXISTS = "E105"

    # Task errors (E2xx)
    TASK_NOT_FOUND = "E200"
    TASK_ALREADY_RUNNING = "E201"
    TASK_NOT_RUNNING = "E202"
    TASK_DEPENDENCY_MISSING = "E210"
    TASK_DEPENDENCY_SELF = "E211"
    TASK_DEPENDENCY_CYCLE = "E212"

    # Session errors (E3xx)
    SESSION_NOT_FOUND = "E300"
    SESSION_EXISTS = "E301"
    SESSION_ALREADY_REGISTERED = "E302"
    SESSION_NOT_REGISTERED = "E303"

    # Hook errors (E4xx)
    HOOK_FAILED = "E400"
    HOOK_TIMEOUT = "E401"

    # CLI errors (E5xx)
    INVALID_ARGUMENT = "E500"
    UNKNOWN_COMMAND = "E501"

    # General errors (E9xx)
    INTERNAL = "E900"


# Message templates — use str.format() to fill in details
MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.CONFIG_NOT_FOUND: "Config file not found: {path}",
    ErrorCode.CONFIG_PARSE_ERROR: "Invalid TOML in {path}: {detail}",
    ErrorCode.CONFIG_INVALID_TASK: "Invalid task definition for '{task}': {detail}",
    ErrorCode.CONFIG_UNKNOWN_KEYS: "Unknown config key(s): {keys}",
    ErrorCode.CONFIG_VALIDATION: "Config validation error: {detail}",
    ErrorCode.CONFIG_ALREADY_EXISTS: "Config already exists: {path}",
    ErrorCode.TASK_NOT_FOUND: "Task '{task}' not found in config",
    ErrorCode.TASK_ALREADY_RUNNING: "Task '{task}' already running",
    ErrorCode.TASK_NOT_RUNNING: "Task '{task}' not running",
    ErrorCode.TASK_DEPENDENCY_MISSING: "Task '{task}' depends on unknown task '{dep}'",
    ErrorCode.TASK_DEPENDENCY_SELF: "Task '{task}' depends on itself",
    ErrorCode.TASK_DEPENDENCY_CYCLE: "Dependency cycle detected involving '{dep}'",
    ErrorCode.SESSION_NOT_FOUND: "Session '{session}' doesn't exist. Run 'taskmux start' first.",
    ErrorCode.SESSION_EXISTS: "Session '{session}' already exists",
    ErrorCode.SESSION_ALREADY_REGISTERED: (
        "Session '{session}' already registered from {existing_path}; "
        "refusing to bind to {new_path}"
    ),
    ErrorCode.SESSION_NOT_REGISTERED: "Session '{session}' is not registered with the daemon",
    ErrorCode.HOOK_FAILED: "Hook failed (exit {exit_code}): {command}",
    ErrorCode.HOOK_TIMEOUT: "Hook timed out ({timeout}s): {command}",
    ErrorCode.INVALID_ARGUMENT: "Invalid argument: {detail}",
    ErrorCode.UNKNOWN_COMMAND: "Unknown command '{command}'. Run 'taskmux --help' for usage.",
    ErrorCode.INTERNAL: "Internal error: {detail}",
}


class TaskmuxError(Exception):
    """Structured error with code, message, and optional details."""

    def __init__(self, code: ErrorCode, **kwargs: str | int | list[str]) -> None:
        self.code = code
        self.details = kwargs
        template = MESSAGES.get(code, str(code))
        # Format with kwargs, falling back to raw template on missing keys
        try:
            self.message = template.format(**kwargs)
        except KeyError:
            self.message = template
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Serialize for JSON output."""
        d: dict = {"ok": False, "error": self.code.value, "message": self.message}
        if self.details:
            d["details"] = {k: v for k, v in self.details.items()}
        return d
