"""Host-wide taskmux configuration at ~/.taskmux/config.toml.

Optional file. Every key has a default. Daemon reads it on startup.

Example ~/.taskmux/config.toml:

    health_check_interval = 30
    api_port = 8765

Schema is intentionally small — extend as new global knobs become useful.
"""

from __future__ import annotations

import tomllib
import warnings
from pathlib import Path
from typing import Any

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ErrorCode, TaskmuxError
from .paths import ensureTaskmuxDir, globalConfigPath


class GlobalConfig(BaseModel):
    """Host-wide taskmux config. Frozen — to mutate, build a new instance."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    health_check_interval: int = Field(
        default=30,
        ge=1,
        description="Seconds between daemon health-check sweeps across all projects.",
    )
    api_port: int = Field(
        default=8765,
        ge=1,
        le=65535,
        description="WebSocket API port the daemon listens on.",
    )


def loadGlobalConfig(path: Path | None = None) -> GlobalConfig:
    """Read ~/.taskmux/config.toml. Returns defaults if missing or empty.

    Unknown keys generate a warning and are dropped (extra='ignore' in model).
    Validation failures raise TaskmuxError(CONFIG_VALIDATION).
    """
    p = path or globalConfigPath()
    if not p.exists():
        return GlobalConfig()
    try:
        raw = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise TaskmuxError(
            ErrorCode.CONFIG_PARSE_ERROR, path=str(p), detail=str(e)
        ) from e

    known = set(GlobalConfig.model_fields.keys())
    unknown = set(raw.keys()) - known
    if unknown:
        warnings.warn(
            f"Unknown global config keys ignored: {sorted(unknown)}",
            stacklevel=2,
        )

    try:
        return GlobalConfig(**raw)
    except ValidationError as e:
        raise TaskmuxError(ErrorCode.CONFIG_VALIDATION, detail=str(e)) from e


def writeGlobalConfig(config: GlobalConfig, path: Path | None = None) -> Path:
    """Write the config back to ~/.taskmux/config.toml (preserves formatting)."""
    p = path or globalConfigPath()
    ensureTaskmuxDir()
    doc = tomlkit.document()
    for field, value in config.model_dump().items():
        doc.add(field, value)
    p.write_text(tomlkit.dumps(doc))
    return p


def updateGlobalConfig(updates: dict[str, Any]) -> GlobalConfig:
    """Read, merge updates, validate, write. Returns the new config."""
    current = loadGlobalConfig().model_dump()
    current.update(updates)
    try:
        new = GlobalConfig(**current)
    except ValidationError as e:
        raise TaskmuxError(ErrorCode.CONFIG_VALIDATION, detail=str(e)) from e
    writeGlobalConfig(new)
    return new
