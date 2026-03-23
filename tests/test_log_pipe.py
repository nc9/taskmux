"""Tests for the log pipe module."""

from pathlib import Path

from taskmux._log_pipe import rotateLogs


class TestRotateLogs:
    def test_rotates_current_to_1(self, tmp_path: Path):
        log = tmp_path / "task.log"
        log.write_text("original")
        rotateLogs(log, max_files=3)
        assert not log.exists()
        assert (tmp_path / "task.log.1").read_text() == "original"

    def test_shifts_existing(self, tmp_path: Path):
        log = tmp_path / "task.log"
        log.write_text("current")
        (tmp_path / "task.log.1").write_text("prev1")
        rotateLogs(log, max_files=3)
        assert (tmp_path / "task.log.1").read_text() == "current"
        assert (tmp_path / "task.log.2").read_text() == "prev1"

    def test_deletes_oldest_beyond_max(self, tmp_path: Path):
        log = tmp_path / "task.log"
        log.write_text("current")
        (tmp_path / "task.log.1").write_text("prev1")
        (tmp_path / "task.log.2").write_text("prev2")
        rotateLogs(log, max_files=2)
        assert (tmp_path / "task.log.1").read_text() == "current"
        assert (tmp_path / "task.log.2").read_text() == "prev1"
        assert not (tmp_path / "task.log.3").exists()

    def test_no_file_to_rotate(self, tmp_path: Path):
        log = tmp_path / "task.log"
        rotateLogs(log, max_files=3)  # should not raise
        assert not log.exists()
