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


class TaskConfig(_StrictConfig):
    """Single task definition."""

    command: str
    auto_start: bool = True


class TaskmuxConfig(_StrictConfig):
    """Top-level taskmux.toml schema."""

    name: str = "taskmux"
    tasks: dict[str, TaskConfig] = {}
