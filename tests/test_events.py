"""Tests for the events module."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from taskmux.events import queryEvents, recordEvent


class TestRecordEvent:
    def test_creates_file(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        with (
            patch("taskmux.events.EVENTS_DIR", tmp_path),
            patch("taskmux.events.EVENTS_FILE", events_file),
        ):
            entry = recordEvent("task_started", session="test", task="server")
            assert entry["event"] == "task_started"
            assert entry["session"] == "test"
            assert entry["task"] == "server"
            assert "ts" in entry

            lines = events_file.read_text().splitlines()
            assert len(lines) == 1
            assert json.loads(lines[0])["event"] == "task_started"

    def test_appends_multiple(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        with (
            patch("taskmux.events.EVENTS_DIR", tmp_path),
            patch("taskmux.events.EVENTS_FILE", events_file),
        ):
            recordEvent("task_started", session="test", task="a")
            recordEvent("task_stopped", session="test", task="b")
            lines = events_file.read_text().splitlines()
            assert len(lines) == 2

    def test_extra_fields(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        with (
            patch("taskmux.events.EVENTS_DIR", tmp_path),
            patch("taskmux.events.EVENTS_FILE", events_file),
        ):
            entry = recordEvent("auto_restart", session="test", task="x", reason="crash")
            assert entry["reason"] == "crash"


class TestQueryEvents:
    def _write_events(self, events_file: Path, events: list[dict]):
        lines = [json.dumps(e) for e in events]
        events_file.write_text("\n".join(lines) + "\n")

    def test_empty_file(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        with patch("taskmux.events.EVENTS_FILE", events_file):
            assert queryEvents() == []

    def test_filter_by_task(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        events = [
            {"ts": "2024-01-01T00:00:00", "event": "a", "session": "s", "task": "x"},
            {"ts": "2024-01-01T00:01:00", "event": "b", "session": "s", "task": "y"},
        ]
        self._write_events(events_file, events)
        with patch("taskmux.events.EVENTS_FILE", events_file):
            results = queryEvents(task="x")
            assert len(results) == 1
            assert results[0]["task"] == "x"

    def test_filter_by_since(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        now = datetime.now(UTC)
        old = (now - timedelta(hours=2)).isoformat()
        recent = (now - timedelta(minutes=5)).isoformat()
        events = [
            {"ts": old, "event": "old", "session": "s"},
            {"ts": recent, "event": "new", "session": "s"},
        ]
        self._write_events(events_file, events)
        with patch("taskmux.events.EVENTS_FILE", events_file):
            results = queryEvents(since=now - timedelta(hours=1))
            assert len(results) == 1
            assert results[0]["event"] == "new"

    def test_limit(self, tmp_path: Path):
        events_file = tmp_path / "events.jsonl"
        events = [
            {"ts": f"2024-01-01T00:0{i}:00", "event": f"e{i}", "session": "s"} for i in range(5)
        ]
        self._write_events(events_file, events)
        with patch("taskmux.events.EVENTS_FILE", events_file):
            results = queryEvents(limit=2)
            assert len(results) == 2
            assert results[0]["event"] == "e3"
            assert results[1]["event"] == "e4"
