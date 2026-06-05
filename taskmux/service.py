"""OS-level supervisor integration — keep the global daemon alive across crashes,
laptop sleep, closed terminals, and reboot.

    macOS → launchd LaunchDaemon  (/Library/LaunchDaemons, system domain)
    Linux → systemd unit          (/etc/systemd/system, generated; manual enable)

Why a *system* service (not a per-user agent): the daemon binds :443/:80 as root,
then drops to the invoking user itself (see ``daemon._drop_privileges``). Two
consequences the generated unit must paper over, because the supervisor isn't
``sudo``:

* ``_drop_privileges`` reads only ``SUDO_UID`` / ``SUDO_GID`` — absent under
  launchd/systemd — so we inject them, else the daemon stays root and runs every
  task (and chowns ``~/.taskmux``) as root.
* ``paths.py`` resolves ``~/.taskmux`` from ``HOME`` at import, *before* the drop.
  As root that's ``/var/root``, so we pin ``HOME`` to the target user's home.

This module only renders + applies units; deciding whether to stop a running
daemon first is the CLI's job (it owns the tested stop path).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

LAUNCHD_LABEL = "com.taskmux.daemon"
LAUNCHD_PLIST_PATH = Path("/Library/LaunchDaemons/com.taskmux.daemon.plist")
SYSTEMD_UNIT_NAME = "taskmux.service"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/taskmux.service")


class ServiceError(Exception):
    """User-actionable failure while installing/removing the supervisor."""


class TargetUser(NamedTuple):
    name: str
    uid: int
    gid: int
    home: str


class Plan(NamedTuple):
    platform: str  # "macos" | "linux"
    path: str  # where the unit file lands
    content: str  # rendered unit/plist
    auto: bool  # True if this module fully loads it (macOS); False = print steps


def detect_platform() -> str:
    """Return 'macos', 'linux', or the raw ``sys.platform`` for anything else."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def resolve_target(*, allow_current_user: bool) -> TargetUser:
    """Resolve the user the daemon should drop to.

    Real installs need root (writing /Library or /etc/systemd); the target user is
    the one behind ``sudo`` (``SUDO_USER``). ``allow_current_user`` (used by
    ``--dry-run``) lets the render run unprivileged against the caller's own user.
    """
    import pwd

    if os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER")
        if not sudo_user or sudo_user == "root":
            raise ServiceError(
                "Run this via `sudo` from your normal user account — taskmux needs "
                "SUDO_USER to know which user to drop privileges to (it's unset, so "
                "you're likely in a pure root shell). Example: `sudo taskmux daemon install`."
            )
        pw = pwd.getpwnam(sudo_user)
        return TargetUser(pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir)
    if allow_current_user:
        pw = pwd.getpwuid(os.getuid())
        return TargetUser(pw.pw_name, pw.pw_uid, pw.pw_gid, pw.pw_dir)
    raise ServiceError(
        "Installing a system supervisor needs root. Re-run: `sudo taskmux daemon install`."
    )


def _resolve_taskmux_exe() -> str:
    """Absolute path to the `taskmux` entry point the unit should exec.

    Prefer the executable next to the running interpreter (we ARE taskmux, so its
    bin dir is authoritative even under sudo's sanitised PATH), then fall back to
    PATH lookup.
    """
    cand = Path(sys.executable).resolve().parent / "taskmux"
    if cand.exists():
        return str(cand)
    found = shutil.which("taskmux")
    if found:
        return str(Path(found).resolve())
    raise ServiceError(
        "Could not locate the `taskmux` executable to point the service at. "
        "Ensure `taskmux` is installed and on PATH (e.g. `uv tool install taskmux`)."
    )


def build_task_path(home: str) -> str:
    """PATH for daemon-spawned tasks. launchd/systemd give a bare PATH, so seed it
    with the common interpreter dirs (homebrew, ~/.local, cargo, bun, volta) that
    exist on this box. Users can hand-edit the generated unit to add more.
    """
    candidates = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        f"{home}/.local/bin",
        f"{home}/.cargo/bin",
        f"{home}/.bun/bin",
        f"{home}/.volta/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for d in candidates:
        if d not in seen and Path(d).is_dir():
            seen.add(d)
            out.append(d)
    return ":".join(out)


def render_launchd_plist(*, exe: str, target: TargetUser, task_path: str) -> str:
    """macOS LaunchDaemon. KeepAlive{SuccessfulExit:false} = relaunch on any
    abnormal exit (crash, SIGKILL, terminal-close SIGHUP) but NOT after a clean
    `taskmux daemon stop` (which exits 0).
    """
    import plistlib

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [exe, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,  # don't respawn faster than 10s on a crash-loop
        "EnvironmentVariables": {
            "HOME": target.home,
            "SUDO_UID": str(target.uid),
            "SUDO_GID": str(target.gid),
            "PATH": task_path,
        },
        # Capture pre-priv-drop crash output that never reaches daemon.log.
        "StandardOutPath": f"{target.home}/.taskmux/launchd.out.log",
        "StandardErrorPath": f"{target.home}/.taskmux/launchd.err.log",
        "WorkingDirectory": target.home,
        "ProcessType": "Interactive",  # keep dev servers off background CPU throttle
    }
    return plistlib.dumps(plist).decode()


def render_systemd_unit(*, exe: str, target: TargetUser, task_path: str) -> str:
    """Linux systemd unit. Runs as root to bind :443/:80, then the daemon drops to
    the target user via the injected SUDO_UID/GID (same shim as macOS).
    Restart=on-failure mirrors launchd's SuccessfulExit:false.

    Environment= and the ExecStart binary are double-quoted so a home/install path
    containing spaces doesn't corrupt the unit (systemd splits ExecStart and
    Environment on whitespace otherwise).
    """
    return (
        "[Unit]\n"
        "Description=taskmux global daemon\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f'Environment="HOME={target.home}"\n'
        f'Environment="SUDO_UID={target.uid}"\n'
        f'Environment="SUDO_GID={target.gid}"\n'
        f'Environment="PATH={task_path}"\n'
        f'ExecStart="{exe}" daemon\n'
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"WorkingDirectory={target.home}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def build_plan(target: TargetUser, platform: str | None = None) -> Plan:
    platform = platform or detect_platform()
    exe = _resolve_taskmux_exe()
    task_path = build_task_path(target.home)
    if platform == "macos":
        content = render_launchd_plist(exe=exe, target=target, task_path=task_path)
        return Plan("macos", str(LAUNCHD_PLIST_PATH), content, auto=True)
    if platform == "linux":
        content = render_systemd_unit(exe=exe, target=target, task_path=task_path)
        return Plan("linux", str(SYSTEMD_UNIT_PATH), content, auto=False)
    raise ServiceError(
        f"No supervisor integration for platform '{platform}'. "
        "Supported: macOS (launchd), Linux (systemd)."
    )


def running_daemon_pid(home: str) -> int | None:
    """Live daemon pid from the target user's pidfile.

    HOME-independent: `taskmux daemon install` runs under sudo, whose HOME may be
    /var/root, so the generic get_daemon_pid() (which keys off the caller's HOME)
    could miss the real daemon owned by the target user.
    """
    pid_path = Path(home) / ".taskmux" / "daemon.pid"
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return None
    except OSError:
        return pid  # alive but not signalable from here


def _ensure_state_dir(target: TargetUser) -> None:
    """Create ~/.taskmux (owned by the target user) before bootstrap.

    launchd does NOT create the parent dirs for StandardOut/ErrorPath, so on a
    machine where the daemon has never run the plist's log paths wouldn't exist
    and the job would fail to spawn.
    """
    state = Path(target.home) / ".taskmux"
    state.mkdir(mode=0o755, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if state.stat().st_uid != target.uid:
            os.chown(state, target.uid, target.gid)


def install_macos(plan: Plan, target: TargetUser) -> dict:
    """Write the plist (root:wheel 0644), then re-bootstrap it into the system
    domain. Idempotent: boots out any prior job first. The CLI is responsible for
    stopping a running daemon beforehand so the freshly bootstrapped one can bind.
    """
    _ensure_state_dir(target)
    path = Path(plan.path)
    path.write_text(plan.content)
    os.chown(path, 0, 0)
    path.chmod(0o644)
    subprocess.run(
        ["launchctl", "bootout", f"system/{LAUNCHD_LABEL}"],
        capture_output=True,
        text=True,
    )
    boot = subprocess.run(
        ["launchctl", "bootstrap", "system", str(path)],
        capture_output=True,
        text=True,
    )
    return {
        "path": str(path),
        "bootstrap_rc": boot.returncode,
        "bootstrap_err": boot.stderr.strip(),
    }


def write_linux_unit(plan: Plan) -> dict:
    """Write the systemd unit file. Enabling is left to the user (printed steps) —
    auto-running systemctl with an unverified unit isn't worth the blast radius."""
    path = Path(plan.path)
    path.write_text(plan.content)
    path.chmod(0o644)
    return {"path": str(path)}


def systemd_enable_commands() -> list[str]:
    return [
        "systemctl daemon-reload",
        f"systemctl enable --now {SYSTEMD_UNIT_NAME}",
    ]


def uninstall() -> dict:
    """Remove whichever supervisor this platform uses. Idempotent."""
    platform = detect_platform()
    if platform == "macos":
        r = subprocess.run(
            ["launchctl", "bootout", f"system/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
        )
        existed = LAUNCHD_PLIST_PATH.exists()
        LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
        return {"platform": platform, "removed": existed, "bootout_rc": r.returncode}
    if platform == "linux":
        subprocess.run(["systemctl", "disable", "--now", SYSTEMD_UNIT_NAME], capture_output=True)
        existed = SYSTEMD_UNIT_PATH.exists()
        SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
        return {"platform": platform, "removed": existed}
    raise ServiceError(f"No supervisor integration for platform '{platform}'.")
