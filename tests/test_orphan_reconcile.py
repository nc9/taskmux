"""Issue 2 — persistent process tracking + orphan reaping (reap + respawn).

macOS leaves setsid'd tasks running when the daemon crashes; a fresh daemon's
in-memory _tasks is empty, so without reconciliation it spawns duplicates. These
tests pin: process-record persistence, the ps-marker identity guard, the _spawn
dup-guard, and startup reconcile_orphans.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from taskmux import supervisor as supmod
from taskmux.models import TaskConfig, TaskmuxConfig
from taskmux.supervisor import PosixSupervisor


def _cfg(tasks: dict[str, str], name: str = "proj") -> TaskmuxConfig:
    parsed = {n: TaskConfig(command=c) for n, c in tasks.items()}
    return TaskmuxConfig(name=name, tasks=parsed)


def _sup(
    tmp: Path, tasks: dict[str, str] | None = None, boot_id: str = "BOOTNEW"
) -> PosixSupervisor:
    sup = PosixSupervisor(_cfg(tasks or {"web": "sleep 60"}, "proj"), boot_id=boot_id)
    sup._state_path = lambda: tmp / "state.json"  # type: ignore[method-assign]
    return sup


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _spawn_marked_orphan(prefix: str, boot: str = "BOOTOLD") -> subprocess.Popen:
    """A real process group whose sh argv carries `prefix+boot` (ps-visible)."""
    cmd = f": {prefix}{boot}; sleep 60"
    proc = subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)
    # Wait until ps actually shows the marker so the identity check is reliable.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True
        )
        if prefix in out.stdout:
            break
        time.sleep(0.05)
    return proc


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=2)


def _write_records(tmp: Path, records: dict) -> None:
    (tmp / "state.json").write_text(json.dumps({"assigned_ports": {}, "running": records}))


# ---------------------------------------------------------------------------
# marker + persistence
# ---------------------------------------------------------------------------


def test_wrap_command_tags_all_tasks(tmp_path):
    sup = _sup(tmp_path, {"web": "echo hi"}, boot_id="abc123")
    wrapped = sup._wrap_command("web", "echo hi")
    assert "taskmux-task:proj:web:abc123" in wrapped
    # subshell (own-line parens) so exec/comments/heredocs can't strip the marker
    assert "(\necho hi\n)" in wrapped


def test_wrap_command_host_task_keeps_port_export(tmp_path):
    cfg = TaskmuxConfig(name="proj", tasks={"web": TaskConfig(command="serve", host="api")})
    sup = PosixSupervisor(cfg, boot_id="b")
    sup._state_path = lambda: tmp_path / "state.json"  # type: ignore[method-assign]
    wrapped = sup._wrap_command("web", "serve")
    assert "taskmux-task:proj:web:b" in wrapped
    assert "export PORT=" in wrapped


@pytest.mark.parametrize(
    "command,expected",
    [
        ("echo hi # trailing comment", "hi"),  # comment must not eat the `)`
        ("printf done", "done"),
        ("true && echo chained", "chained"),
        ("cat <<EOF\nfrom-heredoc\nEOF", "from-heredoc"),  # heredoc delimiter intact
    ],
)
def test_wrapped_command_runs_with_tricky_syntax(tmp_path, command, expected):
    """The subshell wrapper must not break comments, chaining, or heredocs."""
    wrapped = _sup(tmp_path)._wrap_command("web", command)
    out = subprocess.run(["/bin/sh", "-c", wrapped], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert expected in out.stdout


def test_marker_sanitizes_unsafe_chars(tmp_path):
    sup = _sup(tmp_path, boot_id="b")
    sup.project_id = "weird name;rm"
    assert sup._task_marker_prefix("web") == "taskmux-task:weird_name_rm:web:"


def test_save_and_load_running_records_roundtrip(tmp_path):
    sup = _sup(tmp_path)
    sup._running = {"web": {"pid": 1, "pgid": 1, "started_at": 0.0, "boot_id": "BOOTNEW"}}
    sup._save_state()
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["running"]["web"]["pid"] == 1
    assert sup._load_running_records()["web"]["boot_id"] == "BOOTNEW"


def test_load_running_records_absent_returns_empty(tmp_path):
    assert _sup(tmp_path)._load_running_records() == {}


# ---------------------------------------------------------------------------
# reconcile_orphans
# ---------------------------------------------------------------------------


def test_reconcile_reaps_live_prior_boot_orphan(tmp_path):
    sup = _sup(tmp_path, boot_id="BOOTNEW")
    orphan = _spawn_marked_orphan(sup._task_marker_prefix("web"), boot="BOOTOLD")
    try:
        _write_records(
            tmp_path,
            {
                "web": {
                    "pid": orphan.pid,
                    "pgid": orphan.pid,
                    "started_at": 0.0,
                    "boot_id": "BOOTOLD",
                }
            },
        )
        result = asyncio.run(sup.reconcile_orphans())
        assert result["reaped"] == ["web"]
        # Parent here is pytest (not launchd), so the killed orphan lingers as a
        # zombie until reaped; wait() reaps it and confirms it terminated.
        orphan.wait(timeout=3)
        assert orphan.returncode is not None
        assert sup._load_running_records() == {}  # record cleared
    finally:
        _kill(orphan)


def test_reconcile_skips_current_boot_record(tmp_path):
    sup = _sup(tmp_path, boot_id="BOOTNEW")
    orphan = _spawn_marked_orphan(sup._task_marker_prefix("web"), boot="BOOTNEW")
    try:
        _write_records(
            tmp_path,
            {
                "web": {
                    "pid": orphan.pid,
                    "pgid": orphan.pid,
                    "started_at": 0.0,
                    "boot_id": "BOOTNEW",
                }
            },
        )
        result = asyncio.run(sup.reconcile_orphans())
        assert result["reaped"] == []
        assert _alive(orphan.pid)  # ours — not touched
    finally:
        _kill(orphan)


def test_reconcile_does_not_kill_unmarked_pid(tmp_path):
    """A recorded pid whose command lacks the marker (pid reuse) is left alone."""
    sup = _sup(tmp_path, boot_id="BOOTNEW")
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)  # no marker
    try:
        _write_records(
            tmp_path,
            {"web": {"pid": other.pid, "pgid": other.pid, "started_at": 0.0, "boot_id": "BOOTOLD"}},
        )
        result = asyncio.run(sup.reconcile_orphans())
        assert result["reaped"] == []
        assert result["skipped"] == ["web"]
        assert _alive(other.pid)
    finally:
        _kill(other)


def test_reconcile_skips_pid_reused_by_newer_boot(tmp_path):
    """An OLD-boot record whose pid is now a NEWER-boot leader for the same task is spared."""
    sup = _sup(tmp_path, boot_id="BOOTNEW")
    prefix = sup._task_marker_prefix("web")
    newer = _spawn_marked_orphan(prefix, boot="BOOTNEW")  # live leader carries NEW boot
    try:
        _write_records(
            tmp_path,
            {"web": {"pid": newer.pid, "pgid": newer.pid, "started_at": 0.0, "boot_id": "BOOTOLD"}},
        )
        result = asyncio.run(sup.reconcile_orphans())
        assert result["reaped"] == []
        assert result["skipped"] == ["web"]  # full-marker mismatch → not killed
        assert _alive(newer.pid)
    finally:
        _kill(newer)


def test_reconcile_drops_dead_records(tmp_path):
    sup = _sup(tmp_path, boot_id="BOOTNEW")
    dead = subprocess.Popen(["true"])
    dead.wait()
    _write_records(
        tmp_path,
        {"web": {"pid": dead.pid, "pgid": dead.pid, "started_at": 0.0, "boot_id": "BOOTOLD"}},
    )
    result = asyncio.run(sup.reconcile_orphans())
    assert result == {"reaped": [], "skipped": []}


# ---------------------------------------------------------------------------
# _spawn dup-guard
# ---------------------------------------------------------------------------


def test_spawn_reaps_prior_orphan_before_respawn(tmp_path, monkeypatch):
    sup = _sup(tmp_path, {"web": "sleep 60"}, boot_id="BOOTNEW")
    monkeypatch.setattr(supmod, "_logPath", lambda *a, **k: tmp_path / "web.log")
    orphan = _spawn_marked_orphan(sup._task_marker_prefix("web"), boot="BOOTOLD")
    try:
        _write_records(
            tmp_path,
            {
                "web": {
                    "pid": orphan.pid,
                    "pgid": orphan.pid,
                    "started_at": 0.0,
                    "boot_id": "BOOTOLD",
                }
            },
        )

        async def go():
            await sup._spawn("web")

        asyncio.run(go())
        try:
            orphan.wait(timeout=3)  # reap zombie; confirm old copy was killed
            assert orphan.returncode is not None
            rec = sup._running["web"]
            assert rec["boot_id"] == "BOOTNEW"
            assert rec["pid"] != orphan.pid  # a fresh, managed process
            assert _alive(rec["pid"])
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(sup._running["web"]["pgid"], signal.SIGKILL)
    finally:
        _kill(orphan)


def test_spawn_persists_then_clears_record(tmp_path, monkeypatch):
    sup = _sup(tmp_path, {"web": "sleep 60"}, boot_id="BOOTNEW")
    monkeypatch.setattr(supmod, "_logPath", lambda *a, **k: tmp_path / "web.log")

    async def go():
        await sup.start_task("web")
        assert "web" in sup._running
        assert sup._load_running_records()["web"]["boot_id"] == "BOOTNEW"
        await sup.stop_task("web")

    asyncio.run(go())
    assert "web" not in sup._running
    assert sup._load_running_records() == {}


# ---------------------------------------------------------------------------
# exec-style commands keep the marker (subshell leader is never exec-replaced)
# ---------------------------------------------------------------------------


def _spawn_subshell_orphan(prefix: str, body: str, boot: str = "BOOTOLD") -> subprocess.Popen:
    """Mimic _spawn's wrapping: marked leader + user `body` in a subshell."""
    cmd = f": {prefix}{boot}; ( {body} )"
    proc = subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True
        )
        if prefix in out.stdout:
            break
        time.sleep(0.05)
    return proc


def test_reconcile_reaps_exec_orphan_via_subshell_marker(tmp_path):
    """A user `exec` replaces only the subshell; the marked leader survives → reaped."""
    sup = _sup(tmp_path, boot_id="BOOTNEW")
    prefix = sup._task_marker_prefix("web")
    proc = _spawn_subshell_orphan(prefix, "exec sleep 60", boot="BOOTOLD")
    try:
        # Leader keeps the marker even though the subshell exec'd into sleep.
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True
        )
        assert prefix in out.stdout
        _write_records(
            tmp_path,
            {"web": {"pid": proc.pid, "pgid": proc.pid, "started_at": 0.0, "boot_id": "BOOTOLD"}},
        )
        result = asyncio.run(sup.reconcile_orphans())
        assert result["reaped"] == ["web"], result
        proc.wait(timeout=3)
        assert proc.returncode is not None
    finally:
        _kill(proc)


def test_spawned_exec_task_is_identifiable(tmp_path, monkeypatch):
    """End-to-end: a real `exec` task spawned via _spawn stays marker-identifiable."""
    sup = _sup(tmp_path, {"web": "exec sleep 60"}, boot_id="BOOTNEW")
    monkeypatch.setattr(supmod, "_logPath", lambda *a, **k: tmp_path / "web.log")

    async def go():
        await sup._spawn("web")
        pid = sup._running["web"]["pid"]
        assert await sup._pid_is_our_task(pid, "web", sup.boot_id)

    try:
        asyncio.run(go())
    finally:
        with contextlib.suppress(KeyError, ProcessLookupError, OSError):
            os.killpg(sup._running["web"]["pgid"], signal.SIGKILL)


# ---------------------------------------------------------------------------
# per-instance id + atomic write
# ---------------------------------------------------------------------------


def test_supervisors_get_distinct_instance_ids():
    a = PosixSupervisor(_cfg({"web": "x"}))
    b = PosixSupervisor(_cfg({"web": "x"}))
    assert a.boot_id != b.boot_id
    assert len(a.boot_id) >= 8


def test_save_state_is_atomic_no_tmp_left(tmp_path):
    sup = _sup(tmp_path)
    sup._running = {"web": {"pid": 1, "pgid": 1, "started_at": 0.0, "boot_id": "x"}}
    sup._save_state()
    assert (tmp_path / "state.json").exists()
    assert not (tmp_path / "state.json.tmp").exists()
    json.loads((tmp_path / "state.json").read_text())  # valid JSON


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
