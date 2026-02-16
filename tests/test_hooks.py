"""Tests for hook execution."""

from unittest.mock import patch

from taskmux.hooks import runHook


class TestRunHook:
    def test_none_returns_true(self):
        assert runHook(None) is True

    def test_none_with_task_name(self):
        assert runHook(None, task_name="server") is True

    def test_successful_command(self):
        assert runHook("echo hello") is True

    def test_failed_command(self):
        assert runHook("exit 1") is False

    def test_timeout(self):
        with patch(
            "taskmux.hooks.subprocess.run",
            side_effect=__import__("subprocess").TimeoutExpired("cmd", 30),
        ):
            assert runHook("sleep 999") is False

    def test_prints_output(self, capsys):
        runHook("echo hook-output")
        captured = capsys.readouterr()
        assert "hook-output" in captured.out

    def test_prints_label_with_task_name(self, capsys):
        runHook("echo hi", task_name="server")
        captured = capsys.readouterr()
        assert "[server]" in captured.out
