"""Taskmux - Modern tmux development environment manager."""

__version__ = "0.4.0"
__author__ = "Taskmux Contributors"

from .config import addTask, loadConfig, removeTask, writeConfig
from .errors import ErrorCode, TaskmuxError
from .models import HookConfig, TaskConfig, TaskmuxConfig
from .tmux_manager import TmuxManager

__all__ = [
    "ErrorCode",
    "HookConfig",
    "TaskConfig",
    "TaskmuxConfig",
    "TaskmuxError",
    "TmuxManager",
    "addTask",
    "loadConfig",
    "removeTask",
    "writeConfig",
]
