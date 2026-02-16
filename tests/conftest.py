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

SAMPLE_TOML_WITH_HOOKS = """\
name = "hooked-session"

[hooks]
before_start = "echo global-before"
after_stop = "echo global-after"

[tasks.server]
command = "echo 'Starting server...'"

[tasks.server.hooks]
before_start = "echo server-before"

[tasks.watcher]
command = "cargo watch -x check"
auto_start = false
"""

SAMPLE_TOML_GLOBAL_AUTO_START_FALSE = """\
name = "lazy-session"
auto_start = false

[tasks.server]
command = "echo server"
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sample_toml(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML)
    return p


@pytest.fixture
def sample_toml_hooks(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML_WITH_HOOKS)
    return p


@pytest.fixture
def sample_toml_no_auto(config_dir: Path) -> Path:
    p = config_dir / "taskmux.toml"
    p.write_text(SAMPLE_TOML_GLOBAL_AUTO_START_FALSE)
    return p
