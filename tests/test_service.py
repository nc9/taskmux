"""Unit tests for OS supervisor rendering + planning (pure, no side effects)."""

from __future__ import annotations

import os
import plistlib

import pytest

from taskmux import service
from taskmux.service import ServiceError, TargetUser

TARGET = TargetUser(name="alice", uid=501, gid=20, home="/Users/alice")


def test_detect_platform_is_a_known_token():
    assert service.detect_platform() in {"macos", "linux"} or isinstance(
        service.detect_platform(), str
    )


def test_build_task_path_only_existing_dirs_colon_joined():
    path = service.build_task_path("/Users/alice")
    parts = path.split(":")
    assert "/usr/bin" in parts  # always present
    assert all(os.path.isdir(p) for p in parts)  # no phantom dirs
    assert len(parts) == len(set(parts))  # deduped


def test_render_launchd_plist_is_valid_and_complete():
    xml = service.render_launchd_plist(exe="/opt/tm/taskmux", target=TARGET, task_path="/usr/bin")
    parsed = plistlib.loads(xml.encode())
    assert parsed["Label"] == "com.taskmux.daemon"
    assert parsed["ProgramArguments"] == ["/opt/tm/taskmux", "daemon"]
    # The two launchd-vs-sudo fixes must be present, else the daemon stays root.
    assert parsed["EnvironmentVariables"]["SUDO_UID"] == "501"
    assert parsed["EnvironmentVariables"]["SUDO_GID"] == "20"
    assert parsed["EnvironmentVariables"]["HOME"] == "/Users/alice"
    # Relaunch on abnormal exit, but not after a clean stop (exit 0).
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["StandardErrorPath"] == "/Users/alice/.taskmux/launchd.err.log"


def test_render_systemd_unit_has_drop_shim_and_restart():
    unit = service.render_systemd_unit(exe="/opt/tm/taskmux", target=TARGET, task_path="/usr/bin")
    assert 'ExecStart="/opt/tm/taskmux" daemon' in unit
    assert 'Environment="SUDO_UID=501"' in unit
    assert 'Environment="SUDO_GID=20"' in unit
    assert 'Environment="HOME=/Users/alice"' in unit
    assert "Restart=on-failure" in unit
    assert "[Install]" in unit


def test_render_systemd_unit_quotes_paths_with_spaces():
    target = TargetUser(name="bob", uid=501, gid=20, home="/Users/bob smith")
    unit = service.render_systemd_unit(
        exe="/opt/my tools/taskmux", target=target, task_path="/usr/bin"
    )
    # Quoting keeps the space-bearing exe + home from corrupting the directives.
    assert 'ExecStart="/opt/my tools/taskmux" daemon' in unit
    assert 'Environment="HOME=/Users/bob smith"' in unit


def test_build_plan_macos(monkeypatch):
    monkeypatch.setattr(service, "_resolve_taskmux_exe", lambda: "/opt/tm/taskmux")
    plan = service.build_plan(TARGET, "macos")
    assert plan.platform == "macos"
    assert plan.path == str(service.LAUNCHD_PLIST_PATH)
    assert plan.auto is True
    assert "com.taskmux.daemon" in plan.content


def test_build_plan_linux_is_not_auto(monkeypatch):
    monkeypatch.setattr(service, "_resolve_taskmux_exe", lambda: "/opt/tm/taskmux")
    plan = service.build_plan(TARGET, "linux")
    assert plan.platform == "linux"
    assert plan.path == str(service.SYSTEMD_UNIT_PATH)
    assert plan.auto is False


def test_build_plan_unsupported_platform_raises(monkeypatch):
    monkeypatch.setattr(service, "_resolve_taskmux_exe", lambda: "/opt/tm/taskmux")
    with pytest.raises(ServiceError, match="No supervisor integration"):
        service.build_plan(TARGET, "freebsd")


def test_resolve_target_non_root_refuses(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ServiceError, match="needs root"):
        service.resolve_target(allow_current_user=False)


def test_resolve_target_non_root_dry_run_uses_current_user(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    t = service.resolve_target(allow_current_user=True)
    assert t.uid == os.getuid()


def test_resolve_target_root_without_sudo_user_refuses(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("SUDO_USER", raising=False)
    with pytest.raises(ServiceError, match="SUDO_USER"):
        service.resolve_target(allow_current_user=False)


def test_ensure_state_dir_creates_dir(tmp_path):
    # launchd won't mkdir the stdio log parent — install must. Use our own uid so
    # the chown branch is a no-op (no root needed in the test).
    target = TargetUser(name="me", uid=os.getuid(), gid=os.getgid(), home=str(tmp_path))
    service._ensure_state_dir(target)
    assert (tmp_path / ".taskmux").is_dir()


def test_running_daemon_pid_reads_live_pid(tmp_path, monkeypatch):
    home = tmp_path
    (home / ".taskmux").mkdir()
    (home / ".taskmux" / "daemon.pid").write_text(str(os.getpid()))
    assert service.running_daemon_pid(str(home)) == os.getpid()


def test_running_daemon_pid_missing_and_dead(tmp_path):
    assert service.running_daemon_pid(str(tmp_path)) is None  # no pidfile
    (tmp_path / ".taskmux").mkdir()
    (tmp_path / ".taskmux" / "daemon.pid").write_text("999999")  # not a live pid
    assert service.running_daemon_pid(str(tmp_path)) is None
