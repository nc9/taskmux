"""Taskmux — daemon-supervised dev task manager (PTY-backed, no tmux)."""

__version__ = "0.5.0.dev0"
__author__ = "Taskmux Contributors"

from .config import addTask, loadConfig, removeTask, writeConfig
from .errors import ErrorCode, TaskmuxError
from .models import HookConfig, TaskConfig, TaskmuxConfig
from .supervisor import HealthResult, PosixSupervisor, RestartTracker, make_supervisor

__all__ = [
    "ErrorCode",
    "HealthResult",
    "HookConfig",
    "PosixSupervisor",
    "RestartTracker",
    "TaskConfig",
    "TaskmuxConfig",
    "TaskmuxError",
    "addTask",
    "loadConfig",
    "make_supervisor",
    "removeTask",
    "writeConfig",
]
