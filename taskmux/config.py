"""Functional TOML configuration management for Taskmux."""

import sys
import tomllib
from pathlib import Path

import tomlkit

from .models import TaskConfig, TaskmuxConfig

CONFIG_FILENAME = "taskmux.toml"


def configExists(path: Path | None = None) -> bool:
    """Check if config file exists."""
    p = path or Path(CONFIG_FILENAME)
    return p.is_file()


def loadConfig(path: Path | None = None) -> TaskmuxConfig:
    """Load and parse taskmux.toml. Returns defaults if file missing."""
    p = path or Path(CONFIG_FILENAME)
    if not p.is_file():
        return TaskmuxConfig()

    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"Error: Invalid TOML in {p}: {e}")
        sys.exit(1)

    # Convert raw task dicts / bare strings into TaskConfig-compatible dicts
    raw_tasks = raw.get("tasks", {})
    tasks: dict[str, dict] = {}
    for name, val in raw_tasks.items():
        if isinstance(val, str):
            tasks[name] = {"command": val}
        elif isinstance(val, dict):
            tasks[name] = val
        else:
            print(f"Error: invalid task definition for '{name}'")
            sys.exit(1)
    raw["tasks"] = tasks

    return TaskmuxConfig(**raw)


def writeConfig(path: Path | None, config: TaskmuxConfig) -> Path:
    """Write config to TOML. Omits auto_start when True (default)."""
    p = path or Path(CONFIG_FILENAME)

    doc = tomlkit.document()
    doc.add("name", tomlkit.item(config.name))
    doc.add(tomlkit.nl())

    for task_name, task_cfg in config.tasks.items():
        tbl = tomlkit.table(is_super_table=True)
        inner = tomlkit.table()
        inner.add("command", task_cfg.command)
        if not task_cfg.auto_start:
            inner.add("auto_start", False)
        tbl.add(task_name, inner)
        doc.add("tasks", tbl)

    p.write_text(tomlkit.dumps(doc))
    return p


def addTask(path: Path | None, name: str, command: str) -> TaskmuxConfig:
    """Add a task to config and persist."""
    cfg = loadConfig(path)
    new_tasks = dict(cfg.tasks)
    new_tasks[name] = TaskConfig(command=command)
    cfg = TaskmuxConfig(name=cfg.name, tasks=new_tasks)
    writeConfig(path, cfg)
    return cfg


def removeTask(path: Path | None, name: str) -> tuple[TaskmuxConfig, bool]:
    """Remove a task from config and persist. Returns (config, was_removed)."""
    cfg = loadConfig(path)
    if name not in cfg.tasks:
        return cfg, False
    new_tasks = {k: v for k, v in cfg.tasks.items() if k != name}
    cfg = TaskmuxConfig(name=cfg.name, tasks=new_tasks)
    writeConfig(path, cfg)
    return cfg, True
