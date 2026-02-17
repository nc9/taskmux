"""Pydantic models for Taskmux configuration."""

import warnings

from pydantic import BaseModel, ConfigDict, model_validator


class _StrictConfig(BaseModel):
    """Base config: frozen, warns on unknown keys."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _warn_unknown_keys(cls, values: dict) -> dict:
        if not isinstance(values, dict):
            return values
        known = set(cls.model_fields.keys())
        unknown = set(values.keys()) - known
        for key in sorted(unknown):
            warnings.warn(f"Unknown config key: {key!r}", UserWarning, stacklevel=2)
        return values


class HookConfig(_StrictConfig):
    """Lifecycle hooks for tasks or global config."""

    before_start: str | None = None
    after_start: str | None = None
    before_stop: str | None = None
    after_stop: str | None = None


class TaskConfig(_StrictConfig):
    """Single task definition."""

    command: str
    auto_start: bool = True
    cwd: str | None = None
    health_check: str | None = None
    health_interval: int = 10
    health_timeout: int = 5
    health_retries: int = 3
    depends_on: list[str] = []
    hooks: HookConfig = HookConfig()


class TaskmuxConfig(_StrictConfig):
    """Top-level taskmux.toml schema."""

    name: str = "taskmux"
    auto_start: bool = True
    hooks: HookConfig = HookConfig()
    tasks: dict[str, TaskConfig] = {}

    @model_validator(mode="after")
    def _validate_depends_on(self) -> "TaskmuxConfig":
        """Reject unknown depends_on references and cycles."""
        task_names = set(self.tasks.keys())
        for name, cfg in self.tasks.items():
            for dep in cfg.depends_on:
                if dep not in task_names:
                    raise ValueError(f"Task '{name}' depends on unknown task '{dep}'")
                if dep == name:
                    raise ValueError(f"Task '{name}' depends on itself")

        # Cycle detection via DFS
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {n: WHITE for n in task_names}

        def dfs(node: str) -> None:
            color[node] = GRAY
            for dep in self.tasks[node].depends_on:
                if color[dep] == GRAY:
                    raise ValueError(f"Dependency cycle detected involving '{dep}'")
                if color[dep] == WHITE:
                    dfs(dep)
            color[node] = BLACK

        for n in task_names:
            if color[n] == WHITE:
                dfs(n)

        return self
