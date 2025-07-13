"""
Taskmux configuration management.
"""

import json
import sys
from typing import Dict


class TaskmuxConfig:
    """Manages taskmux.json configuration file loading and validation."""

    def __init__(self, config_path: str = "taskmux.json"):
        self.config_path = config_path
        self.config = {}
        self.load_config()

    def load_config(self):
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, "r") as f:
                self.config = json.load(f)
            print(f"✓ Loaded config from {self.config_path}")
        except FileNotFoundError:
            print(f"Error: Config file {self.config_path} not found")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in {self.config_path}: {e}")
            sys.exit(1)

    def save_config(self):
        """Save current configuration to JSON file"""
        with open(self.config_path, "w") as f:
            json.dump(self.config, f, indent=2)

    @property
    def session_name(self) -> str:
        """Get the tmux session name"""
        return self.config.get("name", "taskmux")

    @property
    def tasks(self) -> Dict[str, str]:
        """Get the tasks dictionary"""
        return self.config.get("tasks", {})

    def add_task(self, task_name: str, command: str):
        """Add a new task to the configuration"""
        self.config.setdefault("tasks", {})[task_name] = command
        self.save_config()

    def remove_task(self, task_name: str) -> bool:
        """Remove a task from the configuration. Returns True if task was found and removed."""
        if task_name not in self.tasks:
            return False
        del self.config["tasks"][task_name]
        self.save_config()
        return True
