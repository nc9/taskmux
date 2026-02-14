"""Shared test fixtures."""

from pathlib import Path

import pytest

SAMPLE_TOML = """\
name = "test-session"

[tasks.server]
command = "echo 'Starting server...'"

[tasks.watcher]
command = "cargo watch -x check"
auto_start = false
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_toml(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML)
    return p
