"""Tests for pydantic models."""

import warnings

import pytest
from pydantic import ValidationError

from taskmux.models import TaskConfig, TaskmuxConfig


class TestTaskConfig:
    def test_defaults(self):
        t = TaskConfig(command="echo hi")
        assert t.command == "echo hi"
        assert t.auto_start is True

    def test_auto_start_false(self):
        t = TaskConfig(command="echo hi", auto_start=False)
        assert t.auto_start is False

    def test_frozen(self):
        t = TaskConfig(command="echo hi")
        with pytest.raises(ValidationError):
            t.command = "echo bye"

    def test_unknown_key_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TaskConfig(command="echo hi", bogus="val")
            assert any("bogus" in str(warning.message) for warning in w)


class TestTaskmuxConfig:
    def test_defaults(self):
        c = TaskmuxConfig()
        assert c.name == "taskmux"
        assert c.tasks == {}

    def test_with_tasks(self):
        c = TaskmuxConfig(
            name="my-session",
            tasks={"server": TaskConfig(command="run-server")},
        )
        assert c.name == "my-session"
        assert c.tasks["server"].command == "run-server"

    def test_frozen(self):
        c = TaskmuxConfig()
        with pytest.raises(ValidationError):
            c.name = "changed"

    def test_unknown_key_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TaskmuxConfig(name="x", mystery=42)
            assert any("mystery" in str(warning.message) for warning in w)
