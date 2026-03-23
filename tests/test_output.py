"""Tests for the output module."""

import json

from taskmux.output import is_json_mode, print_result, set_json_mode


class TestJsonMode:
    def test_default_false(self):
        assert is_json_mode() is False

    def test_set_and_get(self):
        set_json_mode(True)
        assert is_json_mode() is True
        set_json_mode(False)
        assert is_json_mode() is False

    def test_print_result_json_mode(self, capsys):
        set_json_mode(True)
        print_result({"ok": True, "task": "server"})
        set_json_mode(False)
        output = capsys.readouterr().out.strip()
        data = json.loads(output)
        assert data["ok"] is True
        assert data["task"] == "server"

    def test_print_result_no_json_mode(self, capsys):
        set_json_mode(False)
        print_result({"ok": True})
        output = capsys.readouterr().out
        assert output == ""  # nothing printed when not in json mode
