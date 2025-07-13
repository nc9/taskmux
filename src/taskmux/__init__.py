"""
Taskmux - Modern tmux development environment manager

A dynamic tmux session manager with libtmux integration, health monitoring,
auto-restart capabilities, and WebSocket API for real-time communication.
"""

__version__ = "2.0.0"
__author__ = "Taskmux Contributors"

from .config import TaskmuxConfig
from .tmux_manager import TmuxManager
from .daemon import TaskmuxDaemon
from .cli import TaskmuxCLI

__all__ = [
    "TaskmuxConfig",
    "TmuxManager", 
    "TaskmuxDaemon",
    "TaskmuxCLI",
]