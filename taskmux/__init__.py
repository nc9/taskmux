"""Taskmux - Modern tmux development environment manager."""

__version__ = "0.2.0"
__author__ = "Taskmux Contributors"

from .config import addTask, loadConfig, removeTask, writeConfig
from .models import HookConfig, TaskConfig, TaskmuxConfig
from .tmux_manager import TmuxManager

__all__ = [
    "HookConfig",
    "TaskConfig",
    "TaskmuxConfig",
    "TmuxManager",
    "addTask",
    "loadConfig",
    "removeTask",
    "writeConfig",
]
