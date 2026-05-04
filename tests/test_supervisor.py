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
    _make_log_annotator,
    _parseSince,
    _parseSize,
    _public_internal_pair,
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

    def test_mark_cap_reached_edge_trigger(self):
        """First call returns True (emit), subsequent return False (suppress)."""
        rt = RestartTracker()
        assert rt.mark_cap_reached("a") is True
        assert rt.mark_cap_reached("a") is False
        assert rt.mark_cap_reached("a") is False

    def test_reset_clears_cap_reached(self):
        """Successful auto-recovery clears the flag so a future cap re-emits."""
        rt = RestartTracker()
        rt.mark_cap_reached("a")
        rt.reset("a")
        assert rt.mark_cap_reached("a") is True

    def test_cap_reached_per_task(self):
        """Edge state is per-task — flagging 'a' must not silence 'b'."""
        rt = RestartTracker()
        rt.mark_cap_reached("a")
        assert rt.mark_cap_reached("b") is True


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

    def test_banner_emitted_first(self, tmp_path: Path):
        log = tmp_path / "out.log"
        w = LogWriter(log, max_bytes=1_000_000, max_files=3, banner="[taskmux] hello")
        w.write(b"first\n")
        w.close()
        lines = log.read_text().splitlines()
        assert lines[0].endswith(" [taskmux] hello")
        assert lines[1].endswith(" first")

    def test_banner_lands_in_active_log_when_existing_near_full(self, tmp_path: Path):
        # Existing log near max_bytes: the banner must rotate the old file out
        # *before* writing, so the active log contains the banner first.
        log = tmp_path / "out.log"
        log.write_text("x" * 180 + "\n")  # near the 200-byte cap
        w = LogWriter(log, max_bytes=200, max_files=3, banner="[taskmux] hello")
        w.close()
        active = log.read_text().splitlines()
        assert len(active) == 1
        assert active[0].endswith(" [taskmux] hello")
        assert (tmp_path / "out.log.1").exists()

    def test_annotator_emits_followup(self, tmp_path: Path):
        log = tmp_path / "out.log"
        ann = _make_log_annotator("https://web.app.localhost", 1234, throttle_s=0.0)
        w = LogWriter(log, max_bytes=1_000_000, max_files=3, annotator=ann)
        w.write(b"vite ready at http://localhost:1234/\n")
        w.write(b"unrelated noise\n")
        w.close()
        lines = log.read_text().splitlines()
        assert len(lines) == 3
        assert lines[0].endswith(" vite ready at http://localhost:1234/")
        assert lines[1].endswith(" [taskmux] ↳ public URL: https://web.app.localhost")
        assert lines[2].endswith(" unrelated noise")

    def test_annotator_not_applied_to_synthetic_lines(self, tmp_path: Path):
        log = tmp_path / "out.log"
        # Banner + annotation must not themselves trigger another annotation
        # (no recursion). Use a banner that contains the trigger substring.
        ann = _make_log_annotator("https://web.app.localhost", 1234, throttle_s=0.0)
        banner = "[taskmux] up on http://localhost:1234"
        w = LogWriter(log, max_bytes=1_000_000, max_files=3, banner=banner, annotator=ann)
        w.close()
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        assert lines[0].endswith(f" {banner}")


class TestLogAnnotator:
    def test_matches_localhost_and_port(self):
        ann = _make_log_annotator("https://web.app.localhost", 1234, throttle_s=0.0)
        assert ann("Local: http://localhost:1234/") is not None
        assert ann("listening on localhost:1234") is not None
        assert ann("nothing here") is None

    def test_port_word_boundary(self):
        # Port 123 should NOT match :1234 (avoid digit-prefix false positives).
        ann = _make_log_annotator("https://x.localhost", 123, throttle_s=0.0)
        assert ann("http://localhost:1234/") is None
        assert ann("http://localhost:123/") is not None
        assert ann("http://localhost:123") is not None

    def test_throttle_suppresses_repeats(self):
        ann = _make_log_annotator("https://x.localhost", 1234, throttle_s=30.0)
        first = ann("localhost:1234")
        second = ann("localhost:1234")
        assert first is not None
        assert second is None

    def test_returned_string_contains_public_url(self):
        ann = _make_log_annotator("https://api.app.localhost", 9999, throttle_s=0.0)
        out = ann("ready: http://localhost:9999")
        assert out is not None
        assert "https://api.app.localhost" in out


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

    def test_host_bound_task_writes_banner(self, tmp_path):
        cfg = _make_config(
            name="myapp",
            tasks={"web": {"command": "echo done", "host": "web"}},
        )
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            await sup.start_task("web")
            tp = sup._tasks.get("web")
            if tp is not None:
                await asyncio.wait_for(tp.proc.wait(), timeout=5)
            await asyncio.sleep(0.3)

        try:
            _run(_go())
            log_file = tmp_path / "myapp__web.log"
            text = log_file.read_text()
            assert "[taskmux] serving https://web.myapp.localhost" in text
            assert "→ http://localhost:" in text
        finally:
            _stop_log_redirect(sup)

    def test_non_host_task_writes_no_banner(self, tmp_path):
        cfg = _make_config(tasks={"plain": "echo nope"})
        sup = _make_supervisor(cfg, tmp_path)
        _redirect_logs(sup, tmp_path)

        async def _go():
            await sup.start_task("plain")
            tp = sup._tasks.get("plain")
            if tp is not None:
                await asyncio.wait_for(tp.proc.wait(), timeout=5)
            await asyncio.sleep(0.3)

        try:
            _run(_go())
            log_file = tmp_path / "test-session__plain.log"
            assert "[taskmux]" not in log_file.read_text()
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

    def test_tcp_failure_past_boot_grace_restarts_after_one_miss(self, tmp_path):
        """Host-bound TCP probe failure past boot_grace fires after a single miss
        (health_retries_tcp=1), not the general health_retries=3."""
        cfg = _make_config(
            tasks={
                "web": {
                    "command": "echo",
                    "host": "web",
                    "restart_policy": RestartPolicy.ON_FAILURE,
                    "boot_grace": 0,  # past boot immediately
                }
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        # Simulate live process record + closed port → TCP probe fails.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup.assigned_ports["web"] = port
        # Fake "process alive" so we go down the health-retry branch, not the
        # process-dead branch.
        fake_proc = MagicMock()
        fake_proc.started_at = 0.0
        sup._tasks["web"] = fake_proc

        async def _ok(_name):
            return {"ok": True}

        with patch.object(sup, "_restart_task_locked", side_effect=_ok) as m:
            _run(sup.auto_restart_tasks())
            m.assert_called_once_with("web")

    def test_tcp_failure_within_boot_grace_does_not_restart(self, tmp_path):
        cfg = _make_config(
            tasks={
                "web": {
                    "command": "echo",
                    "host": "web",
                    "restart_policy": RestartPolicy.ON_FAILURE,
                    "boot_grace": 60,
                }
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup.assigned_ports["web"] = port
        fake_proc = MagicMock()
        import time as _t

        fake_proc.started_at = _t.time()  # just spawned
        sup._tasks["web"] = fake_proc

        with patch.object(sup, "_restart_task_locked") as m:
            _run(sup.auto_restart_tasks())
            # Within boot_grace + health_retries=3, one miss is not enough.
            m.assert_not_called()


class TestProbeUpstreamCache:
    """probe_upstream caches results for 1.5 s and respects cache invalidation."""

    def test_no_host_or_port_returns_no_host(self, tmp_path):
        cfg = _make_config(tasks={"web": "echo"})
        sup = _make_supervisor(cfg, tmp_path)
        result = sup.probe_upstream("web")
        assert not result.ok and result.method == "no_host"

    def test_cache_hit(self, tmp_path):
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        sup = _make_supervisor(cfg, tmp_path)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup.assigned_ports["web"] = port

        first = sup.probe_upstream("web")
        with patch.object(sup, "_probe_tcp") as m:
            second = sup.probe_upstream("web")
            m.assert_not_called()
        assert second is first

    def test_notify_upstream_dead_invalidates(self, tmp_path):
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        sup = _make_supervisor(cfg, tmp_path)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup.assigned_ports["web"] = port

        sup.probe_upstream("web")
        sup.notify_upstream_dead("web")
        with patch.object(
            sup, "_probe_tcp", return_value=HealthResult(False, "tcp", "x", 0.0)
        ) as m:
            sup.probe_upstream("web")
            m.assert_called_once()


class TestStatusState:
    """get_task_status emits a `state` field that distinguishes
    starting / running / unhealthy / stopped."""

    def test_stopped_when_not_running(self, tmp_path):
        cfg = _make_config(tasks={"web": "echo"})
        sup = _make_supervisor(cfg, tmp_path)
        st = sup.get_task_status("web")
        assert st["state"] == "stopped"
        assert st["running"] is False

    def test_running_when_no_host_and_alive(self, tmp_path):
        cfg = _make_config(tasks={"web": "echo"})
        sup = _make_supervisor(cfg, tmp_path)
        sup._tasks["web"] = MagicMock()  # is_task_healthy → proc-alive → ok
        st = sup.get_task_status("web")
        assert st["state"] == "running"

    def test_starting_when_within_boot_grace_and_port_dead(self, tmp_path):
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web", "boot_grace": 60}})
        sup = _make_supervisor(cfg, tmp_path)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup.assigned_ports["web"] = port
        import time as _t

        proc = MagicMock()
        proc.started_at = _t.time()
        sup._tasks["web"] = proc
        st = sup.get_task_status("web")
        assert st["state"] == "starting"

    def test_unhealthy_when_past_boot_grace_and_port_dead(self, tmp_path):
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web", "boot_grace": 0}})
        sup = _make_supervisor(cfg, tmp_path)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        sup.assigned_ports["web"] = port
        proc = MagicMock()
        proc.started_at = 0.0
        sup._tasks["web"] = proc
        st = sup.get_task_status("web")
        assert st["state"] == "unhealthy"

    def test_explicit_health_check_failure_is_not_masked_by_open_port(self, http_server, tmp_path):
        """A failing health_url must dominate state even if the TCP port is open.

        Regression guard: an early version of _compute_state probed TCP
        unconditionally for host-bound tasks and would mark a task `running`
        whose configured HTTP probe was returning 500.
        """
        port, handler = http_server
        handler.status = 500  # http_server is up (port open) but probe fails
        cfg = _make_config(
            tasks={
                "web": {
                    "command": "echo",
                    "host": "web",
                    "health_url": f"http://127.0.0.1:{port}/",
                    "boot_grace": 0,
                }
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        sup.assigned_ports["web"] = port  # TCP probe would pass
        sup._tasks["web"] = MagicMock()
        st = sup.get_task_status("web")
        assert st["healthy"] is False
        assert st["state"] == "unhealthy"


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


# ---------------------------------------------------------------------------
# Public/internal port surface (so agents stop pairing url + internal port)
# ---------------------------------------------------------------------------


class TestPublicInternalPair:
    def test_host_bound_with_port(self):
        assert _public_internal_pair("api", 58353, 443) == (
            443,
            58353,
            "http://127.0.0.1:58353",
        )

    def test_host_bound_no_port_yet(self):
        assert _public_internal_pair("api", None, 443) == (443, None, None)

    def test_non_host_keeps_internal_as_port(self):
        assert _public_internal_pair(None, None, 443) == (None, None, None)

    def test_custom_proxy_port(self):
        assert _public_internal_pair("api", 9000, 8443) == (
            8443,
            9000,
            "http://127.0.0.1:9000",
        )


def _patch_global(monkeypatch, proxy_port: int = 443):
    from taskmux import global_config as gc
    from taskmux import supervisor as supmod

    fake = gc.GlobalConfig(proxy_https_port=proxy_port)
    monkeypatch.setattr(supmod, "loadGlobalConfig", lambda: fake, raising=False)
    # supervisor.py imports loadGlobalConfig lazily inside the methods, so also
    # patch the source module to cover that import path.
    monkeypatch.setattr(gc, "loadGlobalConfig", lambda *a, **kw: fake)


class TestInspectPortShape:
    def test_host_bound_emits_internal_fields(self, tmp_path, monkeypatch):
        _patch_global(monkeypatch, proxy_port=443)
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        sup = _make_supervisor(cfg, tmp_path)
        sup.assigned_ports["web"] = 58353
        info = sup.inspect_task("web")
        assert info["url"] == "https://web.test-session.localhost"
        assert info["port"] == 443
        assert info["internal_port"] == 58353
        assert info["internal_url"] == "http://127.0.0.1:58353"

    def test_host_bound_before_start_has_null_internal(self, tmp_path, monkeypatch):
        _patch_global(monkeypatch, proxy_port=443)
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        sup = _make_supervisor(cfg, tmp_path)
        info = sup.inspect_task("web")
        assert info["port"] == 443
        assert info["internal_port"] is None
        assert info["internal_url"] is None

    def test_non_host_unchanged(self, tmp_path, monkeypatch):
        _patch_global(monkeypatch, proxy_port=443)
        cfg = _make_config(tasks={"job": "echo"})
        sup = _make_supervisor(cfg, tmp_path)
        info = sup.inspect_task("job")
        assert info["url"] is None
        assert info["port"] is None
        assert info["internal_port"] is None
        assert info["internal_url"] is None

    def test_custom_proxy_port_propagates(self, tmp_path, monkeypatch):
        _patch_global(monkeypatch, proxy_port=8443)
        cfg = _make_config(tasks={"web": {"command": "echo", "host": "web"}})
        sup = _make_supervisor(cfg, tmp_path)
        sup.assigned_ports["web"] = 9000
        info = sup.inspect_task("web")
        assert info["port"] == 8443
        assert info["internal_port"] == 9000


class TestListTasksPortShape:
    def test_per_task_shape(self, tmp_path, monkeypatch):
        _patch_global(monkeypatch, proxy_port=443)
        cfg = _make_config(
            tasks={
                "web": {"command": "echo", "host": "web"},
                "job": "echo",
            }
        )
        sup = _make_supervisor(cfg, tmp_path)
        sup.assigned_ports["web"] = 58353
        out = sup.list_tasks()
        rows = {t["name"]: t for t in out["tasks"]}
        assert rows["web"]["port"] == 443
        assert rows["web"]["internal_port"] == 58353
        assert rows["web"]["internal_url"] == "http://127.0.0.1:58353"
        assert rows["job"]["port"] is None
        assert rows["job"]["internal_port"] is None
        assert rows["job"]["internal_url"] is None
