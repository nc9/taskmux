"""Tests for pydantic models."""

import pytest
from pydantic import ValidationError

from taskmux.errors import ErrorCode, TaskmuxError
from taskmux.models import HookConfig, RestartPolicy, TaskConfig, TaskmuxConfig


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

    def test_unknown_key_raises(self):
        with pytest.raises(TaskmuxError, match="bogus") as exc_info:
            HookConfig(bogus="val")  # type: ignore[call-arg]
        assert exc_info.value.code == ErrorCode.CONFIG_UNKNOWN_KEYS


class TestRestartPolicy:
    def test_enum_values(self):
        assert RestartPolicy.NO == "no"
        assert RestartPolicy.ON_FAILURE == "on-failure"
        assert RestartPolicy.ALWAYS == "always"

    def test_str_conversion(self):
        assert str(RestartPolicy.NO) == "no"
        assert str(RestartPolicy.ON_FAILURE) == "on-failure"
        assert str(RestartPolicy.ALWAYS) == "always"

    def test_from_string(self):
        assert RestartPolicy("no") == RestartPolicy.NO
        assert RestartPolicy("on-failure") == RestartPolicy.ON_FAILURE
        assert RestartPolicy("always") == RestartPolicy.ALWAYS

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            RestartPolicy("invalid")


class TestTaskConfig:
    def test_defaults(self):
        t = TaskConfig(command="echo hi")
        assert t.command == "echo hi"
        assert t.auto_start is True
        assert t.cwd is None
        assert t.host is None
        assert t.host_path == "/"
        assert t.health_check is None
        assert t.health_interval == 10
        assert t.health_timeout == 5
        assert t.health_retries == 3
        assert t.stop_grace_period == 5
        assert t.max_restarts == 5
        assert t.restart_backoff == 2.0
        assert t.restart_policy == RestartPolicy.ON_FAILURE
        assert t.depends_on == []
        assert t.hooks == HookConfig()

    def test_restart_policy_no(self):
        t = TaskConfig(command="echo hi", restart_policy="no")
        assert t.restart_policy == RestartPolicy.NO

    def test_restart_policy_always(self):
        t = TaskConfig(command="echo hi", restart_policy="always")
        assert t.restart_policy == RestartPolicy.ALWAYS

    def test_restart_policy_invalid(self):
        with pytest.raises(ValidationError):
            TaskConfig(command="echo hi", restart_policy="invalid")

    def test_new_fields(self):
        t = TaskConfig(
            command="cargo run",
            cwd="apps/api",
            host="api",
            host_path="/health",
            health_check="curl -sf localhost:4000/health",
            health_interval=5,
            stop_grace_period=10,
            max_restarts=3,
            restart_backoff=3.0,
            depends_on=["db"],
        )
        assert t.cwd == "apps/api"
        assert t.host == "api"
        assert t.host_path == "/health"
        assert t.health_check == "curl -sf localhost:4000/health"
        assert t.health_interval == 5
        assert t.stop_grace_period == 10
        assert t.max_restarts == 3
        assert t.restart_backoff == 3.0
        assert t.depends_on == ["db"]

    def test_host_validation_accepts(self):
        for h in ("api", "a", "web-1", "v2", "foo-bar-baz"):
            assert TaskConfig(command="x", host=h).host == h

    def test_host_validation_rejects(self):
        for h in ("Api", "web_1", "-foo", "foo-", "foo.bar", "FOO", "1-", "-1"):
            with pytest.raises(TaskmuxError) as exc_info:
                TaskConfig(command="x", host=h)
            assert exc_info.value.code == ErrorCode.CONFIG_VALIDATION

    def test_old_port_key_rejected(self):
        with pytest.raises(TaskmuxError) as exc_info:
            TaskConfig(command="x", port=8000)  # type: ignore[call-arg]
        assert exc_info.value.code == ErrorCode.CONFIG_UNKNOWN_KEYS

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

    def test_unknown_key_raises(self):
        with pytest.raises(TaskmuxError, match="bogus") as exc_info:
            TaskConfig(command="echo hi", bogus="val")  # type: ignore[call-arg]
        assert exc_info.value.code == ErrorCode.CONFIG_UNKNOWN_KEYS


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

    def test_unknown_key_raises(self):
        with pytest.raises(TaskmuxError, match="mystery") as exc_info:
            TaskmuxConfig(name="x", mystery=42)  # type: ignore[call-arg]
        assert exc_info.value.code == ErrorCode.CONFIG_UNKNOWN_KEYS

    def test_depends_on_unknown_task(self):
        with pytest.raises(TaskmuxError) as exc_info:
            TaskmuxConfig(tasks={"a": TaskConfig(command="echo a", depends_on=["nonexistent"])})
        assert exc_info.value.code == ErrorCode.TASK_DEPENDENCY_MISSING

    def test_depends_on_self(self):
        with pytest.raises(TaskmuxError) as exc_info:
            TaskmuxConfig(tasks={"a": TaskConfig(command="echo a", depends_on=["a"])})
        assert exc_info.value.code == ErrorCode.TASK_DEPENDENCY_SELF

    def test_depends_on_cycle(self):
        with pytest.raises(TaskmuxError) as exc_info:
            TaskmuxConfig(
                tasks={
                    "a": TaskConfig(command="echo a", depends_on=["b"]),
                    "b": TaskConfig(command="echo b", depends_on=["a"]),
                }
            )
        assert exc_info.value.code == ErrorCode.TASK_DEPENDENCY_CYCLE

    def test_depends_on_valid(self):
        c = TaskmuxConfig(
            tasks={
                "db": TaskConfig(command="echo db"),
                "api": TaskConfig(command="echo api", depends_on=["db"]),
                "web": TaskConfig(command="echo web", depends_on=["api"]),
            }
        )
        assert c.tasks["api"].depends_on == ["db"]
        assert c.tasks["web"].depends_on == ["api"]

    def test_name_validation_rejects(self):
        for n in ("My_Project", "foo.bar", "FOO", "-x", "x-", ""):
            with pytest.raises(TaskmuxError) as exc_info:
                TaskmuxConfig(name=n)
            assert exc_info.value.code == ErrorCode.CONFIG_VALIDATION

    def test_duplicate_host_rejected(self):
        with pytest.raises(TaskmuxError) as exc_info:
            TaskmuxConfig(
                tasks={
                    "a": TaskConfig(command="echo a", host="api"),
                    "b": TaskConfig(command="echo b", host="api"),
                }
            )
        assert exc_info.value.code == ErrorCode.CONFIG_VALIDATION

    def test_depends_on_three_node_cycle(self):
        with pytest.raises(TaskmuxError) as exc_info:
            TaskmuxConfig(
                tasks={
                    "a": TaskConfig(command="echo a", depends_on=["c"]),
                    "b": TaskConfig(command="echo b", depends_on=["a"]),
                    "c": TaskConfig(command="echo c", depends_on=["b"]),
                }
            )
        assert exc_info.value.code == ErrorCode.TASK_DEPENDENCY_CYCLE
