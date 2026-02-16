"""Tests for pydantic models."""

import warnings

import pytest
from pydantic import ValidationError

from taskmux.models import HookConfig, TaskConfig, TaskmuxConfig


class TestHookConfig:
    def test_defaults(self):
        h = HookConfig()
        assert h.before_start is None
        assert h.after_start is None
        assert h.before_stop is None
        assert h.after_stop is None

    def test_set_values(self):
        h = HookConfig(before_start="echo hi", after_stop="echo bye")
        assert h.before_start == "echo hi"
        assert h.after_stop == "echo bye"
        assert h.after_start is None

    def test_frozen(self):
        h = HookConfig(before_start="echo hi")
        with pytest.raises(ValidationError):
            h.before_start = "echo bye"

    def test_unknown_key_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            HookConfig(bogus="val")
            assert any("bogus" in str(warning.message) for warning in w)


class TestTaskConfig:
    def test_defaults(self):
        t = TaskConfig(command="echo hi")
        assert t.command == "echo hi"
        assert t.auto_start is True
        assert t.hooks == HookConfig()

    def test_auto_start_false(self):
        t = TaskConfig(command="echo hi", auto_start=False)
        assert t.auto_start is False

    def test_with_hooks(self):
        h = HookConfig(before_start="echo pre")
        t = TaskConfig(command="echo hi", hooks=h)
        assert t.hooks.before_start == "echo pre"

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
        assert c.auto_start is True
        assert c.hooks == HookConfig()
        assert c.tasks == {}

    def test_with_tasks(self):
        c = TaskmuxConfig(
            name="my-session",
            tasks={"server": TaskConfig(command="run-server")},
        )
        assert c.name == "my-session"
        assert c.tasks["server"].command == "run-server"

    def test_global_auto_start_false(self):
        c = TaskmuxConfig(auto_start=False)
        assert c.auto_start is False

    def test_global_hooks(self):
        h = HookConfig(before_start="echo starting")
        c = TaskmuxConfig(hooks=h)
        assert c.hooks.before_start == "echo starting"

    def test_frozen(self):
        c = TaskmuxConfig()
        with pytest.raises(ValidationError):
            c.name = "changed"

    def test_unknown_key_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            TaskmuxConfig(name="x", mystery=42)
            assert any("mystery" in str(warning.message) for warning in w)
