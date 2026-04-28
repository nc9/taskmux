"""Tests for PosixSupervisor — real PTY/subprocess where it's cheap, mocks for
deterministic state-machine/probe-precedence checks."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from taskmux.errors import ErrorCode
from taskmux.models import RestartPolicy, TaskConfig, TaskmuxConfig
from taskmux.supervisor import (
    HealthResult,
    LogWriter,
    PosixSupervisor,
    RestartTracker,
    _parseSince,
    _parseSize,
    make_supervisor,
    readLogFile,
    rotateLogs,
)


def _run(coro):
    """Run an async coro on a fresh loop. Avoids pytest-asyncio dependency."""
    return asyncio.run(coro)


def _make_config(name: str = "test-session", **kwargs) -> TaskmuxConfig:
    tasks = kwargs.pop("tasks", {})
    parsed = {
        k: TaskConfig(**v) if isinstance(v, dict) else TaskConfig(command=v)
        for k, v in tasks.items()
    }
    return TaskmuxConfig(name=name, tasks=parsed, **kwargs)


def _make_supervisor(cfg: TaskmuxConfig, tmp_path: Path | None = None) -> PosixSupervisor:
    sup = PosixSupervisor(cfg, config_dir=tmp_path)
    # Redirect state file into a temp dir so tests don't touch ~/.taskmux/.
    if tmp_path is not None:
        sup._state_path = lambda: tmp_path / "state.json"  # type: ignore[method-assign]
    return sup


def _redirect_logs(sup: PosixSupervisor, tmp_path: Path) -> None:
    """Force log files into tmp_path so tests don't write to ~/.taskmux/."""
    import taskmux.supervisor as supmod

    def fake(session, task, task_cfg, worktree_id=None) -> Path:  # type: ignore[no-untyped-def]
        if task_cfg.log_file:
            return Path(task_cfg.log_file).expanduser()
        return tmp_path / f"{session}__{task}.log"

    sup._test_log_patch = patch.object(supmod, "_logPath", side_effect=fake)  # type: ignore[attr-defined]
    sup._test_log_patch.start()


def _stop_log_redirect(sup: PosixSupervisor) -> None:
    p = getattr(sup, "_test_log_patch", None)
    if p is not None:
        p.stop()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestParseSize:
    def test_units(self):
        assert _parseSize("10MB") == 10 * 1024 * 1024
        assert _parseSize("500KB") == 500 * 1024
        assert _parseSize("1GB") == 1024**3
        assert _parseSize("100B") == 100
        assert _parseSize("1024") == 1024


class TestParseSince:
    def test_duration(self):
        from datetime import UTC, datetime, timedelta

        before = datetime.now(UTC) - timedelta(seconds=300)
        result = _parseSince("5m")
        assert abs((result - before).total_seconds()) < 2

    def test_invalid(self):
        from taskmux.errors import TaskmuxError

        with pytest.raises(TaskmuxError):
            _parseSince("garbage")


# ---------------------------------------------------------------------------
# RestartTracker
# ---------------------------------------------------------------------------


class TestRestartTracker:
    def test_record_increments(self):
        rt = RestartTracker()
        assert rt.get("a")["count"] == 0
        rt.record("a")
        rt.record("a")
        assert rt.get("a")["count"] == 2

    def test_reset(self):
        rt = RestartTracker()
        rt.record("a")
        rt.reset("a")
        assert rt.get("a")["count"] == 0

    def test_health_failures(self):
        rt = RestartTracker()
        assert rt.record_health_failure("a") == 1
        assert rt.record_health_failure("a") == 2
        rt.reset_health_failures("a")
        assert rt.record_health_failure("a") == 1

    def test_manually_stopped(self):
        rt = RestartTracker()
        assert not rt.is_manually_stopped("a")
        rt.mark_manually_stopped("a")
        assert rt.is_manually_stopped("a")
        rt.clear_manually_stopped("a")
        assert not rt.is_manually_stopped("a")

    def test_last_health(self):
        rt = RestartTracker()
        result = HealthResult(True, "tcp", None, 0.0)
        rt.record_health_result("a", result)
        assert rt.last_health("a") == result


# ---------------------------------------------------------------------------
# Log rotation + writer
# ---------------------------------------------------------------------------


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
        rotateLogs(log, max_files=3)
        assert not log.exists()


class TestLogWriter:
    def test_appends_lines_with_timestamps(self, tmp_path: Path):
        log = tmp_path / "out.log"
        w = LogWriter(log, max_bytes=1_000_000, max_files=3)
        w.write(b"hello\nworld\n")
        w.close()
        lines = log.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].endswith(" hello")
        assert lines[1].endswith(" world")
        # timestamp prefix shape: 2024-01-01T00:00:00.123
        assert lines[0][4] == "-" and lines[0][10] == "T"

    def test_partial_line_buffered(self, tmp_path: Path):
        log = tmp_path / "out.log"
        w = LogWriter(log, max_bytes=1_000_000, max_files=3)
        w.write(b"part")
        # Nothing flushed yet (no newline).
        assert log.read_text() == ""
        w.write(b"ial\n")
        w.close()
        assert log.read_text().rstrip().endswith("partial")

    def test_rotates_at_max_bytes(self, tmp_path: Path):
        log = tmp_path / "out.log"
        w = LogWriter(log, max_bytes=200, max_files=3)
        for i in range(20):
            w.write(f"line-{i:04d}-padding-padding-padding\n".encode())
        w.close()
        assert (tmp_path / "out.log.1").exists()


class TestReadLogFile:
    def test_grep_filter(self, tmp_path: Path):
        log = tmp_path / "out.log"
        log.write_text(
            "2024-01-01T00:00:00.000 alpha\n"
            "2024-01-01T00:00:01.000 beta\n"
            "2024-01-01T00:00:02.000 alpha-2\n"
        )
        out = readLogFile(log, lines=10, grep="alpha", since=None)
        assert len(out) == 2
        assert all("alpha" in line for line in out)

    def test_lines_tail(self, tmp_path: Path):
        log = tmp_path / "out.log"
        log.write_text("\n".join(f"line-{i}" for i in range(20)) + "\n")
        out = readLogFile(log, lines=5, grep=None, since=None)
        assert out == [f"line-{i}" for i in range(15, 20)]


# ---------------------------------------------------------------------------
# Health probes (kept identical in spirit to test_health_probes.py)
# ---------------------------------------------------------------------------


class _SilentHandler(BaseHTTPRequestHandler):
    body = b'<html><body><div id="__next">app</div></body></html>'
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):  # noqa: A002
        pass


@pytest.fixture
def http_server():
    handler = type("H", (_SilentHandler,), {})
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, handler
    finally:
        server.shutdown()
        server.server_close()


class TestHealthProbes:
    def test_http_ok(self, http_server, tmp_path):
        port, _ = http_server
        sup = _make_supervisor(_make_config(tasks={"web": "echo"}), tmp_path)
        result = sup._probe_http(f"http://127.0.0.1:{port}/", 2.0, 200, None)
        assert result.ok and result.method == "http"

    def test_http_status_mismatch(self, http_server, tmp_path):
        port, handler = http_server
        handler.status = 500
        sup = _make_supervisor(_make_config(tasks={"web": "echo"}), tmp_path)
        result = sup._probe_http(f"http://127.0.0.1:{port}/", 2.0, 200, None)
        assert not result.ok and "500" in (result.reason or "")

    def test_tcp_open(self, http_server, tmp_path):
        port, _ = http_server
        sup = _make_supervisor(_make_config(tasks={"web": "echo"}), tmp_path)
        assert sup._probe_tcp(port, 1.0).ok

    def test_tcp_closed(self, tmp_path):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup = _make_supervisor(_make_config(tasks={"web": "echo"}), tmp_path)
        result = sup._probe_tcp(port, 1.0)
        assert not result.ok

    def test_shell(self, tmp_path):
        sup = _make_supervisor(_make_config(tasks={"web": "echo"}), tmp_path)
        assert sup._probe_shell("true", 1.0).ok
        assert not sup._probe_shell("false", 1.0).ok


class TestHealthPrecedence:
    def test_url_beats_shell(self, http_server, tmp_path):
        port, _ = http_server
        cfg = _make_config(
            tasks={
                "web": {
                    "command": "echo",
                    "health_url": f"http://127.0.0.1:{port}/",
                    "health_check": "false",
                }
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        result = sup.check_health("web")
        assert result.ok and result.method == "http"

    def test_shell_beats_tcp(self, tmp_path):
        cfg = _make_config(
            tasks={"web": {"command": "echo", "health_check": "true", "host": "web"}}
        )
        sup = _make_supervisor(cfg, tmp_path)
        sup.assigned_ports["web"] = 1
        result = sup.check_health("web")
        assert result.ok and result.method == "shell"

    def test_tcp_when_only_host(self, http_server, tmp_path):
        port, _ = http_server
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        sup = _make_supervisor(cfg, tmp_path)
        sup.assigned_ports["web"] = port
        result = sup.check_health("web")
        assert result.ok and result.method == "tcp"

    def test_proc_fallback_running(self, tmp_path):
        cfg = _make_config(tasks={"web": "echo"})
        sup = _make_supervisor(cfg, tmp_path)
        sup._tasks["web"] = MagicMock()
        result = sup.check_health("web")
        assert result.ok and result.method == "proc"

    def test_proc_fallback_not_running(self, tmp_path):
        cfg = _make_config(tasks={"web": "echo"})
        sup = _make_supervisor(cfg, tmp_path)
        result = sup.check_health("web")
        assert not result.ok and result.method == "proc"

    def test_unknown_task(self, tmp_path):
        sup = _make_supervisor(_make_config(tasks={"web": "echo"}), tmp_path)
        result = sup.check_health("ghost")
        assert not result.ok and result.method == "none"


# ---------------------------------------------------------------------------
# Lifecycle (real subprocess+PTY end-to-end)
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_start_unknown_task(self, tmp_path):
        sup = _make_supervisor(_make_config(tasks={"a": "echo a"}), tmp_path)
        _redirect_logs(sup, tmp_path)
        try:
            result = _run(sup.start_task("ghost"))
            assert not result["ok"]
            assert result["error_code"] == ErrorCode.TASK_NOT_FOUND.value
        finally:
            _stop_log_redirect(sup)

    def test_start_then_already_running(self, tmp_path):
        cfg = _make_config(tasks={"sleeper": "sleep 5"})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            r1 = await sup.start_task("sleeper")
            assert r1["ok"]
            r2 = await sup.start_task("sleeper")
            assert not r2["ok"]
            assert r2["error_code"] == ErrorCode.TASK_ALREADY_RUNNING.value
            await sup.stop_all()

        try:
            _run(_go())
        finally:
            _stop_log_redirect(sup)

    def test_start_writes_log(self, tmp_path):
        cfg = _make_config(tasks={"hello": "echo hello-from-task"})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            await sup.start_task("hello")
            tp = sup._tasks.get("hello")
            if tp is not None:
                await asyncio.wait_for(tp.proc.wait(), timeout=5)
            await asyncio.sleep(0.3)

        try:
            _run(_go())
            log_file = tmp_path / "test-session__hello.log"
            assert log_file.exists()
            assert "hello-from-task" in log_file.read_text()
        finally:
            _stop_log_redirect(sup)

    def test_stop_kills_process_group(self, tmp_path):
        cfg = _make_config(tasks={"sleeper": {"command": "sleep 60", "stop_grace_period": 1}})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        result_holder = {}

        async def _go():
            await sup.start_task("sleeper")
            assert "sleeper" in sup._tasks
            result_holder["pid"] = sup._tasks["sleeper"].proc.pid
            result_holder["stop"] = await sup.stop_task("sleeper")

        try:
            _run(_go())
            assert result_holder["stop"]["ok"]
            assert "sleeper" not in sup._tasks
            with pytest.raises(OSError):
                os.kill(result_holder["pid"], 0)
        finally:
            _stop_log_redirect(sup)

    def test_kill_immediate(self, tmp_path):
        cfg = _make_config(tasks={"sleeper": "sleep 60"})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            await sup.start_task("sleeper")
            return await sup.kill_task("sleeper")

        try:
            result = _run(_go())
            assert result["ok"]
            assert "sleeper" not in sup._tasks
        finally:
            _stop_log_redirect(sup)

    def test_restart_replaces_process(self, tmp_path):
        cfg = _make_config(tasks={"sleeper": {"command": "sleep 60", "stop_grace_period": 1}})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            await sup.start_task("sleeper")
            pid1 = sup._tasks["sleeper"].proc.pid
            await sup.restart_task("sleeper")
            pid2 = sup._tasks["sleeper"].proc.pid
            await sup.stop_all()
            return pid1, pid2

        try:
            pid1, pid2 = _run(_go())
            assert pid1 != pid2
        finally:
            _stop_log_redirect(sup)

    def test_pty_makes_isatty_true(self, tmp_path):
        marker = tmp_path / "tty.txt"
        cfg = _make_config(
            tasks={
                "tty": {
                    "command": (
                        f'python3 -c "import sys; '
                        f"open('{marker}', 'w').write(str(sys.stdout.isatty()))\""
                    ),
                }
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            await sup.start_task("tty")
            tp = sup._tasks.get("tty")
            if tp is not None:
                await asyncio.wait_for(tp.proc.wait(), timeout=5)

        try:
            _run(_go())
            assert marker.read_text() == "True"
        finally:
            _stop_log_redirect(sup)

    def test_stop_all_then_start_all(self, tmp_path):
        cfg = _make_config(
            tasks={
                "a": {"command": "sleep 60", "stop_grace_period": 1},
                "b": {"command": "sleep 60", "stop_grace_period": 1},
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            r = await sup.start_all()
            assert r["ok"]
            assert set(sup._tasks.keys()) == {"a", "b"}
            await sup.stop_all()
            return dict(sup._tasks)

        try:
            tasks_after = _run(_go())
            assert tasks_after == {}
        finally:
            _stop_log_redirect(sup)


# ---------------------------------------------------------------------------
# Concurrency guard (R-002 regression)
# ---------------------------------------------------------------------------


class TestConcurrencyGuards:
    """Per-task lock prevents duplicate spawns under concurrent RPCs."""

    def test_concurrent_start_for_same_task(self, tmp_path):
        cfg = _make_config(tasks={"sleeper": {"command": "sleep 30", "stop_grace_period": 1}})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            r1, r2 = await asyncio.gather(sup.start_task("sleeper"), sup.start_task("sleeper"))
            await sup.stop_all()
            return r1, r2

        try:
            r1, r2 = _run(_go())
            # Exactly one succeeds; the other gets TASK_ALREADY_RUNNING.
            outcomes = sorted([r1.get("ok", False), r2.get("ok", False)])
            assert outcomes == [False, True]
        finally:
            _stop_log_redirect(sup)


# ---------------------------------------------------------------------------
# Auto-restart state machine
# ---------------------------------------------------------------------------


class TestAutoRestart:
    def test_skips_when_policy_no(self, tmp_path):
        cfg = _make_config(tasks={"a": {"command": "echo", "restart_policy": RestartPolicy.NO}})
        sup = _make_supervisor(cfg, tmp_path)
        with patch.object(sup, "_restart_task_locked") as m:
            _run(sup.auto_restart_tasks())
            m.assert_not_called()

    def test_skips_manually_stopped(self, tmp_path):
        cfg = _make_config(tasks={"a": {"command": "echo", "restart_policy": RestartPolicy.ALWAYS}})
        sup = _make_supervisor(cfg, tmp_path)
        sup.restart_tracker.mark_manually_stopped("a")
        with patch.object(sup, "_restart_task_locked") as m:
            _run(sup.auto_restart_tasks())
            m.assert_not_called()

    def test_restarts_when_process_dead(self, tmp_path):
        cfg = _make_config(
            tasks={"a": {"command": "echo", "restart_policy": RestartPolicy.ON_FAILURE}}
        )
        sup = _make_supervisor(cfg, tmp_path)

        async def _ok(_name):
            return {"ok": True}

        with patch.object(sup, "_restart_task_locked", side_effect=_ok) as m:
            _run(sup.auto_restart_tasks())
            m.assert_called_once_with("a")

    def test_max_restarts_respected(self, tmp_path):
        cfg = _make_config(
            tasks={
                "a": {
                    "command": "echo",
                    "restart_policy": RestartPolicy.ALWAYS,
                    "max_restarts": 2,
                },
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        sup.restart_tracker._data["a"] = {"count": 2.0, "last": 0.0}
        with patch.object(sup, "_restart_task_locked") as m:
            _run(sup.auto_restart_tasks())
            m.assert_not_called()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    def test_posix_returns_supervisor(self, tmp_path):
        cfg = _make_config(tasks={"a": "echo"})
        sup = make_supervisor(cfg, config_dir=tmp_path)
        assert isinstance(sup, PosixSupervisor)

    def test_windows_raises(self, tmp_path):
        cfg = _make_config(tasks={"a": "echo"})
        with (
            patch("taskmux.supervisor.platform.system", return_value="Windows"),
            pytest.raises(NotImplementedError),
        ):
            make_supervisor(cfg, config_dir=tmp_path)
