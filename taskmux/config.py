"""Functional TOML configuration management for Taskmux."""

import sys
import tomllib
from pathlib import Path

import tomlkit

from .models import HookConfig, TaskConfig, TaskmuxConfig

CONFIG_FILENAME = "taskmux.toml"


def configExists(path: Path | None = None) -> bool:
    """Check if config file exists."""
    p = path or Path(CONFIG_FILENAME)
    return p.is_file()


def _parseHooks(raw: dict) -> dict:
    """Extract hook fields from a raw dict, returning only non-None values."""
    hook_fields = {"before_start", "after_start", "before_stop", "after_stop"}
    return {k: v for k, v in raw.items() if k in hook_fields and v is not None}


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

    # Parse global hooks
    global_hooks = raw.pop("hooks", {})

    # Convert raw task dicts / bare strings into TaskConfig-compatible dicts
    raw_tasks = raw.get("tasks", {})
    tasks: dict[str, dict] = {}
    for name, val in raw_tasks.items():
        if isinstance(val, str):
            tasks[name] = {"command": val}
        elif isinstance(val, dict):
            task_dict = dict(val)
            # Extract nested hooks table
            task_hooks = task_dict.pop("hooks", {})
            if task_hooks:
                task_dict["hooks"] = _parseHooks(task_hooks)
            tasks[name] = task_dict
        else:
            print(f"Error: invalid task definition for '{name}'")
            sys.exit(1)
    raw["tasks"] = tasks

    if global_hooks:
        raw["hooks"] = _parseHooks(global_hooks)

    return TaskmuxConfig(**raw)


def _writeHooksTable(hooks: HookConfig) -> tomlkit.items.Table | None:  # type: ignore[name-defined]
    """Build a tomlkit table for hooks, returning None if all empty."""
    fields = [
        ("before_start", hooks.before_start),
        ("after_start", hooks.after_start),
        ("before_stop", hooks.before_stop),
        ("after_stop", hooks.after_stop),
    ]
    non_empty = [(k, v) for k, v in fields if v is not None]
    if not non_empty:
        return None
    tbl = tomlkit.table()
    for k, v in non_empty:
        tbl.add(k, v)
    return tbl


def writeConfig(path: Path | None, config: TaskmuxConfig) -> Path:
    """Write config to TOML. Omits defaults (auto_start=True, empty hooks)."""
    p = path or Path(CONFIG_FILENAME)

    doc = tomlkit.document()
    doc.add("name", tomlkit.item(config.name))

    if not config.auto_start:
        doc.add("auto_start", tomlkit.item(False))

    doc.add(tomlkit.nl())

    # Global hooks
    hooks_tbl = _writeHooksTable(config.hooks)
    if hooks_tbl:
        doc.add("hooks", hooks_tbl)
        doc.add(tomlkit.nl())

    for task_name, task_cfg in config.tasks.items():
        tbl = tomlkit.table(is_super_table=True)
        inner = tomlkit.table()
        inner.add("command", task_cfg.command)
        if not task_cfg.auto_start:
            inner.add("auto_start", False)
        if task_cfg.cwd is not None:
            inner.add("cwd", task_cfg.cwd)
        if task_cfg.health_check is not None:
            inner.add("health_check", task_cfg.health_check)
        if task_cfg.health_interval != 10:
            inner.add("health_interval", task_cfg.health_interval)
        if task_cfg.health_timeout != 5:
            inner.add("health_timeout", task_cfg.health_timeout)
        if task_cfg.health_retries != 3:
            inner.add("health_retries", task_cfg.health_retries)
        if task_cfg.depends_on:
            inner.add("depends_on", task_cfg.depends_on)
        # Task-level hooks
        task_hooks_tbl = _writeHooksTable(task_cfg.hooks)
        if task_hooks_tbl:
            inner.add("hooks", task_hooks_tbl)
        tbl.add(task_name, inner)
        doc.add("tasks", tbl)

    p.write_text(tomlkit.dumps(doc))
    return p


def addTask(
    path: Path | None,
    name: str,
    command: str,
    *,
    cwd: str | None = None,
    health_check: str | None = None,
    depends_on: list[str] | None = None,
) -> TaskmuxConfig:
    """Add a task to config and persist."""
    cfg = loadConfig(path)
    new_tasks = dict(cfg.tasks)
    kwargs: dict = {"command": command}
    if cwd is not None:
        kwargs["cwd"] = cwd
    if health_check is not None:
        kwargs["health_check"] = health_check
    if depends_on:
        kwargs["depends_on"] = depends_on
    new_tasks[name] = TaskConfig(**kwargs)
    cfg = TaskmuxConfig(name=cfg.name, auto_start=cfg.auto_start, hooks=cfg.hooks, tasks=new_tasks)
    writeConfig(path, cfg)
    return cfg


def removeTask(path: Path | None, name: str) -> tuple[TaskmuxConfig, bool]:
    """Remove a task from config and persist. Returns (config, was_removed)."""
    cfg = loadConfig(path)
    if name not in cfg.tasks:
        return cfg, False
    new_tasks = {k: v for k, v in cfg.tasks.items() if k != name}
    cfg = TaskmuxConfig(name=cfg.name, auto_start=cfg.auto_start, hooks=cfg.hooks, tasks=new_tasks)
    writeConfig(path, cfg)
    return cfg, True
