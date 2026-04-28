"""Unified multi-project daemon for Taskmux.

A single daemon process per host manages all registered projects:
  - Each project's `taskmux.toml` is loaded into its own TaskmuxCLI/TmuxManager.
  - The registry at ~/.taskmux/registry.json is watched for add/remove events.
  - Each project's config file is watched independently.
  - Health-check loop iterates all projects, applies per-task restart policy.
  - WebSocket API serves session-scoped requests + cross-project queries.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import websockets
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .events import recordEvent
from .host_resolver import HostResolver, getResolver
from .paths import (
    REGISTRY_PATH,
    ensureTaskmuxDir,
    globalDaemonLogPath,
    globalDaemonPidPath,
)
from .proxy import ProxyServer
from .registry import readRegistry
from .url import taskUrl

if TYPE_CHECKING:
    from .cli import TaskmuxCLI


# ---------------------------------------------------------------------------
# PID-file helpers (global daemon)
# ---------------------------------------------------------------------------


def get_daemon_pid() -> int | None:
    """Return live global daemon PID, else None. Cleans stale pid file."""
    pid_path = globalDaemonPidPath()
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        with contextlib.suppress(OSError):
            pid_path.unlink()
        return None
    except OSError:
        return pid


def _write_daemon_pid() -> None:
    ensureTaskmuxDir()
    globalDaemonPidPath().write_text(str(os.getpid()))


def _clear_daemon_pid() -> None:
    pid_path = globalDaemonPidPath()
    with contextlib.suppress(OSError):
        if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()


# ---------------------------------------------------------------------------
# Per-project config watcher
# ---------------------------------------------------------------------------


class ConfigWatcher(FileSystemEventHandler):
    """Watches a single project's taskmux.toml and reloads its CLI on change."""

    def __init__(
        self,
        cli: TaskmuxCLI,
        loop: asyncio.AbstractEventLoop,
        on_reload: callable | None = None,  # type: ignore[type-arg]
        on_missing: callable | None = None,  # type: ignore[type-arg]
    ):
        self.cli = cli
        self.target_path = str(cli.config_path)
        self.loop = loop
        self.on_reload = on_reload
        self.on_missing = on_missing
        self.logger = logging.getLogger("taskmux-daemon")

    def _matches(self, event: FileSystemEvent) -> bool:
        if str(event.src_path) == self.target_path:
            return True
        # os.replace / atomic rename can fire moved events with dest=target.
        dest = getattr(event, "dest_path", None)
        return dest is not None and str(dest) == self.target_path

    def on_modified(self, event: FileSystemEvent) -> None:
        if not self._matches(event):
            return
        self.loop.call_soon_threadsafe(self._reload_safe)

    def on_created(self, event: FileSystemEvent) -> None:
        if not self._matches(event):
            return
        self.loop.call_soon_threadsafe(self._reload_safe)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Renamed onto the target → reload; renamed away → mark missing.
        dest = getattr(event, "dest_path", None)
        if dest is not None and str(dest) == self.target_path:
            self.loop.call_soon_threadsafe(self._reload_safe)
        elif str(event.src_path) == self.target_path:
            self.loop.call_soon_threadsafe(self._missing_safe)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if str(event.src_path) != self.target_path:
            return
        self.loop.call_soon_threadsafe(self._missing_safe)

    def _reload_safe(self) -> None:
        try:
            self.cli.reload_config()
            recordEvent("config_reloaded", session=self.cli.config.name)
            self.logger.info(f"Reloaded config for '{self.cli.config.name}'")
            if self.on_reload:
                self.on_reload(self.cli)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Failed to reload config at {self.target_path}: {e}")

    def _missing_safe(self) -> None:
        self.logger.warning(
            f"Config for '{self.cli.config.name}' disappeared at {self.target_path}"
        )
        if self.on_missing:
            self.on_missing(self.cli)


# ---------------------------------------------------------------------------
# Registry watcher
# ---------------------------------------------------------------------------


class RegistryWatcher(FileSystemEventHandler):
    """Watches ~/.taskmux/registry.json and notifies the daemon to re-sync."""

    def __init__(self, daemon: TaskmuxDaemon, loop: asyncio.AbstractEventLoop):
        self.daemon = daemon
        self.loop = loop
        self.target_path = str(REGISTRY_PATH)

    def _matches(self, event: FileSystemEvent) -> bool:
        # os.replace fires a moved event with dest_path = target.
        if str(event.src_path) == self.target_path:
            return True
        dest = getattr(event, "dest_path", None)
        return dest is not None and str(dest) == self.target_path

    def _schedule_sync(self) -> None:
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.daemon._sync_with_registry())
        )

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()

    def on_created(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()

    def on_moved(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self._schedule_sync()


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class TaskmuxDaemon:
    """Unified multi-project daemon."""

    def __init__(self, api_port: int | None = None):
        from .global_config import loadGlobalConfig

        self.global_config = loadGlobalConfig()
        # Explicit api_port arg wins over global config so --port still works.
        self.api_port = api_port if api_port is not None else self.global_config.api_port
        self.running = False
        self.health_check_interval = self.global_config.health_check_interval
        self.health_check_task: asyncio.Task | None = None
        self.websocket_clients: set = set()
        self.projects: dict[str, TaskmuxCLI] = {}
        self.observers: dict[str, Observer] = {}  # type: ignore[reportInvalidTypeForm]
        self.registry_observer: Observer | None = None  # type: ignore[reportInvalidTypeForm]
        self.project_states: dict[str, str] = {}  # session -> "ok" | "config_missing" | "error"
        self.project_paths: dict[str, str] = {}  # session -> abs config_path
        self.proxy: ProxyServer | None = None
        # Proxy is "eligible" once CA is installed and config allows it; we may
        # not have started the listener yet (no registered projects → nothing
        # to certify). First eligible project register triggers the bind.
        self._proxy_eligible = False
        self._proxy_started = False
        # Pre-bound listening socket for :443. Opened in start() while we still
        # have root, then handed to ProxyServer after privilege drop.
        self._proxy_sock: socket.socket | None = None
        # Pluggable hostname resolution (writes /etc/hosts by default; can
        # also run an in-process DNS server). Constructed during start().
        self.host_resolver: HostResolver | None = None
        # In-process DNS server (only when host_resolver = "dns_server").
        # Lifetime tied to the daemon's asyncio loop. None for other resolvers.
        self.dns_server: object | None = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.logger = self._setup_logging()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    # ---- logging ----

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("taskmux-daemon")
        logger.setLevel(logging.INFO)
        # Don't propagate to root — root may have its own handlers (basicConfig
        # set by deps), which would duplicate every record into our file/console.
        logger.propagate = False
        # Idempotent: drop any prior handlers before re-attaching.
        for h in list(logger.handlers):
            logger.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()

        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # When detached, the spawn redirects stderr → daemon.log already, so a
        # console (stderr) handler would double every line in the file. Only
        # attach the console handler when running in a real terminal.
        if sys.stderr.isatty():
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console.setFormatter(formatter)
            logger.addHandler(console)

        ensureTaskmuxDir()
        file_h = logging.FileHandler(globalDaemonLogPath())
        file_h.setLevel(logging.DEBUG)
        file_h.setFormatter(formatter)
        logger.addHandler(file_h)
        return logger

    def _signal_handler(self, signum, frame) -> None:  # noqa: ARG002
        self.logger.info(f"Received signal {signum}, shutting down...")
        self.stop()
        sys.exit(0)

    # ---- lifecycle ----

    async def start(self) -> None:
        existing = get_daemon_pid()
        if existing is not None and existing != os.getpid():
            self.logger.error(f"Global daemon already running (pid {existing})")
            sys.exit(1)

        self._warn_if_unprivileged()

        # Step 1: while we may still have root, pre-bind the privileged proxy
        # socket. This must happen before _drop_privileges() since :443 needs
        # CAP_NET_BIND_SERVICE / root.
        self._proxy_sock = self._pre_bind_proxy_socket()

        # Step 2: also while privileged, do whatever the resolver needs at
        # root level. For `etc_hosts` this writes the managed block. For
        # `dns_server` this writes /etc/resolver/<tld> (or platform equivalent).
        self._install_resolver_root()

        # Step 3: drop privileges back to the user that ran sudo. Everything
        # after this point — tmux ops, mkcert minting, state files, pid file,
        # the DNS server itself — runs as the user, so paths and file
        # ownership are correct.
        self._drop_privileges()

        _write_daemon_pid()
        self.running = True
        self._loop = asyncio.get_running_loop()

        self.logger.info(f"Starting unified taskmux daemon (pid {os.getpid()}, uid {os.getuid()})")

        await self._sync_with_registry()
        self._start_registry_watcher()
        await self._maybe_start_dns_server()
        self._sync_hostnames()
        await self._maybe_start_proxy()

        self.health_check_task = asyncio.create_task(self._health_check_loop())
        api_task = asyncio.create_task(self._start_api_server())

        self.logger.info(f"Daemon ready on port {self.api_port} ({len(self.projects)} project(s))")

        try:
            await asyncio.gather(self.health_check_task, api_task)
        except asyncio.CancelledError:
            self.logger.info("Daemon tasks cancelled")

    def stop(self) -> None:
        self.running = False

        if self.registry_observer is not None:
            with contextlib.suppress(Exception):
                self.registry_observer.stop()
                self.registry_observer.join(timeout=2)

        for session, observer in list(self.observers.items()):
            with contextlib.suppress(Exception):
                observer.stop()
                observer.join(timeout=2)
            self.observers.pop(session, None)

        if self.health_check_task and not self.health_check_task.done():
            self.health_check_task.cancel()

        if self.proxy is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self.proxy.stop(), self._loop)

        if self.dns_server is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self.dns_server.stop(), self._loop)  # type: ignore[attr-defined]

        _clear_daemon_pid()
        self.logger.info("Taskmux daemon stopped")

    # ---- registry sync ----

    def _start_registry_watcher(self) -> None:
        if self._loop is None:
            return
        ensureTaskmuxDir()
        observer = Observer()
        observer.schedule(
            RegistryWatcher(self, self._loop),
            str(REGISTRY_PATH.parent),
            recursive=False,
        )
        observer.start()
        self.registry_observer = observer
        self.logger.info(f"Watching registry at {REGISTRY_PATH}")

    async def _sync_with_registry(self) -> None:
        """Diff in-memory projects against registry on disk; add/remove as needed."""
        async with self._lock:
            on_disk = readRegistry()
            current = set(self.projects.keys())
            wanted = set(on_disk.keys())

            for session in wanted - current:
                entry = on_disk[session]
                self._register_locked(session, Path(entry["config_path"]))

            for session in current - wanted:
                self._unregister_locked(session)

    def _register_locked(self, session: str, config_path: Path) -> None:
        """Register a project. Caller must hold self._lock."""
        from .cli import TaskmuxCLI

        # Always remember the path so config_missing entries can surface it.
        self.project_paths[session] = str(config_path)

        if session in self.projects:
            return
        if not config_path.exists():
            self.logger.warning(f"Registry entry for '{session}' points to missing {config_path}")
            self.project_states[session] = "config_missing"
            return
        try:
            cli = TaskmuxCLI(config_path=config_path)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Failed to load '{session}' from {config_path}: {e}")
            self.project_states[session] = "error"
            return

        if cli.config.name != session:
            self.logger.warning(
                f"Registry session '{session}' != config name '{cli.config.name}' "
                f"for {config_path}; using registry key"
            )

        self.projects[session] = cli
        self.project_states[session] = "ok"

        # Wire route updates from TmuxManager to the proxy regardless of proxy state.
        cli.tmux.on_task_route_change = self._on_task_route_change
        if self._proxy_eligible and self.proxy is not None:
            self._mint_and_register_proxy(session, cli)
            # Late bind: if proxy was eligible but had no projects yet, bind now.
            if self._loop is not None and not self._proxy_started:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._start_proxy_listener())
                )
        # Refresh host resolver mappings. For dns_server this is a pure
        # in-memory update (no privilege needed). For etc_hosts post-drop
        # this no-ops with EACCES; we log a hint so the user knows.
        if self.host_resolver is not None:
            new_hosts = [
                f"{tc.host}.{cli.config.name}.{self.global_config.dns_managed_tld}"
                for tc in cli.config.tasks.values()
                if tc.host is not None
            ]
            self._sync_hostnames()
            if new_hosts and self.host_resolver.name == "etc_hosts":
                self.logger.info(
                    f"Project '{session}' added with hosts {new_hosts}. "
                    f"Restart `sudo taskmux daemon` to refresh /etc/hosts "
                    f'(or use host_resolver = "dns_server" for dynamic adds).'
                )

        if self._loop is not None:
            observer = Observer()
            handler = ConfigWatcher(
                cli,
                self._loop,
                on_reload=lambda c, s=session: self._on_project_reload(s),
                on_missing=lambda c, s=session: self._loop.call_soon_threadsafe(  # type: ignore[union-attr]
                    lambda: asyncio.create_task(self._mark_missing(s))
                ),
            )
            observer.schedule(handler, str(config_path.parent), recursive=False)
            observer.start()
            self.observers[session] = observer

        self.logger.info(f"Registered project '{session}' from {config_path}")

    def _unregister_locked(self, session: str) -> None:
        """Unregister a project. Caller must hold self._lock."""
        observer = self.observers.pop(session, None)
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.stop()
                observer.join(timeout=2)
        self.projects.pop(session, None)
        self.project_states.pop(session, None)
        self.project_paths.pop(session, None)
        if self.proxy is not None:
            self.proxy.unregister_project(session)
            from .ca import dropCert

            with contextlib.suppress(Exception):
                dropCert(session)
        # Refresh the resolver so the unregistered project's hosts disappear
        # (dns_server: instant; etc_hosts: no-op post-drop).
        self._sync_hostnames()
        self.logger.info(f"Unregistered project '{session}'")

    # ---- privileged bootstrap ----

    def _warn_if_unprivileged(self) -> None:
        """Loudly warn at startup when the daemon won't be able to bind privileged
        ports or write to system files (the actual failures still happen + log
        their own errors, but they're scattered and easy to miss in a tail)."""
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            return
        if not self.global_config.proxy_enabled:
            return
        # POSIX: euid 0 means we have full privileges. Windows has no euid;
        # we let the bind/write attempts surface their own messages there.
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if is_root:
            return
        needs_priv: list[str] = []
        if self.global_config.proxy_https_port < 1024:
            needs_priv.append(f"binding the proxy on :{self.global_config.proxy_https_port}")
        if self.global_config.host_resolver in ("etc_hosts", "dns_server"):
            target = (
                "/etc/hosts"
                if self.global_config.host_resolver == "etc_hosts"
                else f"/etc/resolver/{self.global_config.dns_managed_tld}"
            )
            needs_priv.append(f"writing {target}")
        if not needs_priv:
            return
        self.logger.error(
            "Daemon started WITHOUT root — the following will fail: "
            + "; ".join(needs_priv)
            + ". Run `sudo taskmux daemon` (the daemon binds privileged "
            "resources as root, then drops to your user). To run unprivileged "
            "anyway: set proxy_enabled = false (or proxy_https_port >= 1024 + "
            'host_resolver = "noop") in ~/.taskmux/config.toml.'
        )

    def _pre_bind_proxy_socket(self) -> socket.socket | None:
        """Open + listen on the proxy port while we still have root.

        Returns the listening socket; ProxyServer wraps it in TLS later.
        Returns None when proxy is disabled, port is non-privileged, or bind
        fails (logged). Safe to call as a non-root user — bind to a high port
        will succeed without privileges.
        """
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            return None
        if not self.global_config.proxy_enabled:
            return None
        family = socket.AF_INET
        addr = (self.global_config.proxy_bind, self.global_config.proxy_https_port)
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(addr)
            s.listen(128)
            s.setblocking(False)
        except (PermissionError, OSError) as e:
            s.close()
            self.logger.error(
                f"Pre-bind to {addr[0]}:{addr[1]} failed: {e}. "
                f"Run with `sudo taskmux daemon` (the daemon binds :443 as root "
                f"then drops privileges immediately) or set proxy_https_port to "
                f"a non-privileged port (>=1024) in ~/.taskmux/config.toml."
            )
            return None
        self.logger.info(
            f"Pre-bound proxy listener on {addr[0]}:{addr[1]} (will TLS-wrap after privilege drop)"
        )
        return s

    def _install_resolver_root(self) -> None:
        """While privileged, do the resolver-specific one-time install:

        - etc_hosts: write a managed block now (will need re-sync later
          but we're going to lose privilege so do it once).
        - dns_server: write /etc/resolver/<tld> (or the platform's
          equivalent) so the OS sends queries to our soon-to-start
          in-process DNS server.
        - noop: nothing.
        """
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            return
        if not self.global_config.proxy_enabled:
            return

        kind = self.global_config.host_resolver
        if kind == "etc_hosts":
            # Build the etc_hosts resolver up-front and let it write while root.
            try:
                self.host_resolver = getResolver("etc_hosts")
            except ValueError as e:
                self.logger.error(f"Host resolver init failed: {e}")
                return
            mappings = self._collect_host_mappings()
            try:
                self.host_resolver.sync(mappings)
            except (PermissionError, OSError) as e:
                self.logger.warning(
                    f"etc_hosts sync failed ({e}). Run with sudo or set "
                    f'host_resolver = "noop" in ~/.taskmux/config.toml.'
                )
        elif kind == "dns_server":
            from . import dns_install

            try:
                dns_install.installDelegation(
                    self.global_config.dns_managed_tld,
                    self.global_config.dns_server_port,
                )
                dns_install.flushDnsCache()
            except (PermissionError, OSError, RuntimeError) as e:
                self.logger.error(
                    f"DNS delegation install failed: {e}. "
                    f"Browsers won't resolve {self.global_config.dns_managed_tld} "
                    f"hostnames until this is fixed (or switch to "
                    f'host_resolver = "etc_hosts").'
                )

    async def _maybe_start_dns_server(self) -> None:
        """Post-privilege-drop: start the in-process DNS server if configured."""
        if self.global_config.host_resolver != "dns_server":
            return
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            return

        from .dns_server import DnsServer

        srv = DnsServer(
            host="127.0.0.1",
            port=self.global_config.dns_server_port,
            tld=self.global_config.dns_managed_tld,
        )
        try:
            await srv.start()
        except OSError as e:
            self.logger.error(
                f"DNS server bind to 127.0.0.1:{self.global_config.dns_server_port} "
                f"failed: {e}. Likely something else (dnsmasq / pihole) is on this "
                f"port — change dns_server_port in ~/.taskmux/config.toml."
            )
            return
        self.dns_server = srv
        self.host_resolver = getResolver("dns_server", dns_server=srv)

    def _collect_host_mappings(self) -> list[tuple[str, str]]:
        """Walk the registry; return every (fqdn, 127.0.0.1) for tasks with host."""
        from .config import loadConfig
        from .registry import readRegistry

        mappings: list[tuple[str, str]] = []
        for session, entry in readRegistry().items():
            cfg_path = Path(entry["config_path"])
            if not cfg_path.exists():
                continue
            try:
                cfg = loadConfig(cfg_path)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"Skipping host collection for {session!r}: {e}")
                continue
            for task_cfg in cfg.tasks.values():
                if task_cfg.host is None:
                    continue
                fqdn = f"{task_cfg.host}.{cfg.name}.{self.global_config.dns_managed_tld}"
                mappings.append((fqdn, "127.0.0.1"))
        return mappings

    def _sync_hostnames(self) -> None:
        """Push the current set of mappings into whichever resolver is active.

        Cheap to call — for `etc_hosts` after-drop this will likely fail with
        EACCES (which we already wrote while root). For `dns_server` it's a
        pure in-memory map update; we call this on every project register /
        unregister / config-reload.
        """
        if self.host_resolver is None:
            return
        mappings = self._collect_host_mappings()
        # etc_hosts post-drop sync is expected to fail with EACCES — the root
        # bootstrap already populated the file; suppression intended.
        with contextlib.suppress(PermissionError, OSError):
            self.host_resolver.sync(mappings)

    def _drop_privileges(self) -> None:
        """If running as root via sudo, drop to SUDO_UID/SUDO_GID.

        Without this, libtmux talks to /tmp/tmux-0 (root's tmux server) and
        can't see the user's sessions, mkcert writes root-owned cert files,
        and ~/.taskmux/ resolves under root's HOME. We bind :443 first, then
        come back down to the invoking user.
        """
        # geteuid is POSIX-only; on Windows we don't drop privileges (the daemon
        # either runs as Admin or doesn't bind privileged ports — there's no
        # sudo equivalent to demote from).
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if not sudo_uid or not sudo_gid:
            self.logger.warning(
                "Daemon is running as root with no SUDO_UID — staying as root. "
                "tmux/state/cert ownership will be off; prefer `sudo taskmux daemon`."
            )
            return
        import pwd as _pwd

        uid = int(sudo_uid)
        gid = int(sudo_gid)
        pw = _pwd.getpwuid(uid)
        # Heal any prior root-owned state under the user's ~/.taskmux/ before
        # dropping privileges — pid file, cert dirs, etc. left behind by a
        # daemon that ran without dropping privs would otherwise EACCES the
        # newly-unprivileged daemon.
        taskmux_dir = Path(pw.pw_dir) / ".taskmux"
        if taskmux_dir.is_dir():
            for entry in taskmux_dir.rglob("*"):
                with contextlib.suppress(OSError):
                    if entry.stat().st_uid != uid:
                        os.chown(entry, uid, gid, follow_symlinks=False)
            with contextlib.suppress(OSError):
                if taskmux_dir.stat().st_uid != uid:
                    os.chown(taskmux_dir, uid, gid, follow_symlinks=False)
        os.initgroups(pw.pw_name, gid)
        os.setgid(gid)
        os.setuid(uid)
        # Reset env so child processes (mkcert, hooks, …) and HOME-derived
        # paths resolve under the original user.
        os.environ["HOME"] = pw.pw_dir
        os.environ["USER"] = pw.pw_name
        os.environ["LOGNAME"] = pw.pw_name
        # paths.py captured TASKMUX_DIR at import — re-evaluate so it points
        # at the user's ~/.taskmux instead of /var/root/.taskmux.
        from . import paths as _paths

        _paths.TASKMUX_DIR = Path(pw.pw_dir) / ".taskmux"
        _paths.EVENTS_FILE = _paths.TASKMUX_DIR / "events.jsonl"
        _paths.PROJECTS_DIR = _paths.TASKMUX_DIR / "projects"
        _paths.CERTS_DIR = _paths.TASKMUX_DIR / "certs"
        _paths.REGISTRY_PATH = _paths.TASKMUX_DIR / "registry.json"
        _paths.GLOBAL_DAEMON_PID = _paths.TASKMUX_DIR / "daemon.pid"
        _paths.GLOBAL_DAEMON_LOG = _paths.TASKMUX_DIR / "daemon.log"
        _paths.GLOBAL_CONFIG_PATH = _paths.TASKMUX_DIR / "config.toml"
        # daemon.py imported REGISTRY_PATH directly; refresh.
        global REGISTRY_PATH
        REGISTRY_PATH = _paths.REGISTRY_PATH
        self.logger.info(f"Dropped privileges: now running as {pw.pw_name} (uid={uid}, gid={gid})")

    # ---- proxy ----

    async def _maybe_start_proxy(self) -> None:
        """Prepare the HTTPS proxy: verify CA install + build ProxyServer +
        mint certs for known projects.

        The actual listener bind is deferred to _start_proxy_listener, which
        is called once we have at least one project with a cert. That way
        `taskmux daemon` works on an empty registry and only attaches the
        TLS listener when there's something to serve.

        On every daemon start, `mkcert -install` is invoked. It's idempotent —
        a no-op when the CA is already trusted — but acts as a check that
        the trust store still has our CA (e.g. a system update may have
        cleared it). Failure is logged but doesn't kill the proxy; certs
        will be served untrusted until `taskmux ca install` is run manually
        in an interactive session.
        """
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            self.logger.info("Proxy disabled via TASKMUX_DISABLE_PROXY=1")
            return
        if not self.global_config.proxy_enabled:
            self.logger.info("Proxy disabled in global config")
            return
        from .ca import MkcertMissing, ensureCAInstalled

        try:
            ensureCAInstalled()
        except MkcertMissing as e:
            self.logger.warning(f"Proxy not started: {e.message}")
            return
        except Exception as e:  # noqa: BLE001
            # mkcert -install failed (e.g. denied keychain prompt, no GUI
            # session for first-time install). Don't kill the proxy — log
            # and proceed with potentially-untrusted certs.
            self.logger.warning(
                f"CA install/verify failed: {e}. Run `taskmux ca install` "
                f"interactively to trust the local CA. Browsers will show "
                f"a warning until then."
            )

        self.proxy = ProxyServer(
            https_port=self.global_config.proxy_https_port,
            bind=self.global_config.proxy_bind,
            sock=self._proxy_sock,
        )
        self._proxy_eligible = True
        # Mint certs for projects already registered at startup, then bind if any.
        async with self._lock:
            for session, cli in list(self.projects.items()):
                self._mint_and_register_proxy(session, cli)
                cli.tmux.on_task_route_change = self._on_task_route_change
            await self._start_proxy_listener()

    async def _start_proxy_listener(self) -> None:
        """Bind the proxy listener if not yet bound and at least one project has a cert."""
        if self.proxy is None or self._proxy_started:
            return
        if not self.proxy._projects:
            return
        try:
            await self.proxy.start()
        except (PermissionError, OSError) as e:
            self.logger.error(
                f"Proxy bind to :{self.global_config.proxy_https_port} failed: {e}. "
                f"Use `sudo` (daemon must run as root for :443) or "
                f"`setcap cap_net_bind_service+ep $(readlink -f $(which python3))` on Linux."
            )
            self.proxy = None
            self._proxy_eligible = False
            return
        self._proxy_started = True
        self.logger.info(
            f"Proxy bound on https://{self.global_config.proxy_bind}:"
            f"{self.global_config.proxy_https_port}"
        )

    def _mint_and_register_proxy(self, session: str, cli: TaskmuxCLI) -> None:
        """Mint cert + register project + seed routes for already-assigned ports."""
        if self.proxy is None:
            return
        from .ca import mintCert

        try:
            cert, key = mintCert(session)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"mkcert failed for '{session}': {e}")
            return
        self.proxy.register_project(session, cert, key)
        # Seed routes only for tasks whose tmux window is actually alive.
        # Sticky port assignments in state.json can outlive the process; if
        # we route blindly, a different process binding that port later
        # would receive proxied traffic addressed to the trusted URL.
        running_windows: set[str] = set()
        try:
            if cli.tmux.session_exists():
                running_windows = set(cli.tmux.list_windows())
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"list_windows failed for {session!r}: {e}")
        for task_name, task_cfg in cli.config.tasks.items():
            if task_cfg.host is None:
                continue
            port = cli.tmux.assigned_ports.get(task_name)
            if port is not None and task_name in running_windows:
                self.proxy.set_route(session, task_cfg.host, port)

    def _on_task_route_change(self, project: str, _task: str, host: str, port: int | None) -> None:
        """TmuxManager callback: task started (port=N) or stopped (port=None)."""
        if self.proxy is None:
            return
        if port is None:
            self.proxy.drop_route(project, host)
        else:
            self.proxy.set_route(project, host, port)

    async def _resync_project_routes(self, session: str) -> dict:
        """Reconcile a project's proxy routes from disk state + live tmux panes.

        Used after an out-of-band CLI lifecycle command (start/stop/restart/kill)
        in a separate process — that process owns its own TmuxManager and won't
        emit route callbacks here. We re-read assigned_ports from state.json,
        then for each task with a host: route up if its window is alive, drop
        otherwise.
        """
        async with self._lock:
            cli = self.projects.get(session)
        if cli is None:
            return {"ok": False, "error": "unknown_session", "added": [], "dropped": []}
        cli.tmux.reload_state()
        added: list[str] = []
        dropped: list[str] = []
        running_windows: set[str] = set()
        try:
            if cli.tmux.session_exists():
                running_windows = set(cli.tmux.list_windows())
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"resync: list_windows failed for {session!r}: {e}")
        for task_name, task_cfg in cli.config.tasks.items():
            if task_cfg.host is None:
                continue
            port = cli.tmux.assigned_ports.get(task_name)
            if task_name in running_windows and port is not None:
                if self.proxy is not None:
                    self.proxy.set_route(session, task_cfg.host, port)
                added.append(task_cfg.host)
            else:
                if self.proxy is not None:
                    self.proxy.drop_route(session, task_cfg.host)
                dropped.append(task_cfg.host)
        return {"ok": True, "added": added, "dropped": dropped}

    async def _mark_missing(self, session: str) -> None:
        """Mark a project as config_missing — drop live CLI + proxy + DNS state."""
        async with self._lock:
            observer = self.observers.pop(session, None)
            if observer is not None:
                with contextlib.suppress(Exception):
                    observer.stop()
                    observer.join(timeout=2)
            self.projects.pop(session, None)
            self.project_states[session] = "config_missing"
            # Drop proxy routes + cert for this session — keeping them around
            # would let the trusted URL keep resolving to whatever stale port
            # the assignments map remembers.
            if self.proxy is not None:
                self.proxy.unregister_project(session)
                from .ca import dropCert

                with contextlib.suppress(Exception):
                    dropCert(session)
        # Sync host resolver so DNS map / etc_hosts forgets this project too.
        self._sync_hostnames()
        self.logger.info(f"Project '{session}' marked config_missing — health checks paused")

    def _on_project_reload(self, session: str) -> None:
        """ConfigWatcher reload callback: refresh proxy routes + host mappings.

        Runs in the watchdog thread → schedule the actual work on the loop.
        """
        if self._loop is None:
            return

        async def _do() -> None:
            await self._resync_project_routes(session)
            self._sync_hostnames()

        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(_do()))

    # ---- health loop ----

    async def _health_check_loop(self) -> None:
        while self.running:
            try:
                async with self._lock:
                    snapshot = list(self.projects.items())
                for session, cli in snapshot:
                    try:
                        if cli.tmux.session_exists():
                            cli.tmux.auto_restart_tasks()
                    except Exception as e:  # noqa: BLE001
                        self.logger.error(f"Health check error for '{session}': {e}")

                if self.websocket_clients:
                    payload = await self._aggregate_status()
                    await self._broadcast_to_clients({"type": "health_check", "data": payload})

                await asyncio.sleep(self.health_check_interval)
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"Health check loop error: {e}")
                await asyncio.sleep(5)

    # ---- WebSocket API ----

    async def _start_api_server(self) -> None:
        async def handle_client(websocket) -> None:  # type: ignore[type-arg]
            self.websocket_clients.add(websocket)
            self.logger.info(f"WebSocket client connected: {websocket.remote_address}")
            try:
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        response = await self._handle_api_request(data)
                        await websocket.send(json.dumps(response))
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({"error": "Invalid JSON"}))
                    except Exception as e:  # noqa: BLE001
                        await websocket.send(json.dumps({"error": str(e)}))
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self.websocket_clients.discard(websocket)
                self.logger.info(f"WebSocket client disconnected: {websocket.remote_address}")

        async with websockets.serve(handle_client, "localhost", self.api_port):  # type: ignore[arg-type]
            await asyncio.Future()

    KNOWN_COMMANDS = frozenset(
        {
            "list_projects",
            "status_all",
            "status",
            "restart",
            "kill",
            "logs",
            "proxy_routes",
            "url",
            "resync",
        }
    )

    async def _handle_api_request(self, data: dict) -> dict:
        command = data.get("command")
        params = data.get("params", {}) or {}

        if command not in self.KNOWN_COMMANDS:
            return {"error": "unknown_command", "command": command}

        if command == "list_projects":
            return {"command": command, "projects": await self._list_projects()}

        if command == "status_all":
            return {"command": command, "data": await self._aggregate_status()}

        if command == "proxy_routes":
            routes = self.proxy.routes_snapshot() if self.proxy is not None else {}
            return {"command": command, "running": self.proxy is not None, "routes": routes}

        # Session-scoped commands
        session = params.get("session")
        if not session:
            return {"error": "missing_session", "command": command}
        cli = self.projects.get(session)
        if cli is None:
            return {"error": "unknown_session", "session": session, "command": command}

        if command == "status":
            return {"command": command, "session": session, "data": self._project_status(cli)}

        if command == "resync":
            # CLI just changed task lifecycle out-of-band (started/stopped/killed
            # in a separate process). Re-read assigned_ports from disk and
            # reconcile proxy routes against actual tmux pane state.
            return {
                "command": command,
                "session": session,
                "data": await self._resync_project_routes(session),
            }

        if command == "url":
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            task_cfg = cli.config.tasks.get(task_name)
            if task_cfg is None:
                return {"error": "unknown_task", "session": session, "task": task_name}
            if task_cfg.host is None:
                return {"command": command, "session": session, "task": task_name, "url": None}
            return {
                "command": command,
                "session": session,
                "task": task_name,
                "url": taskUrl(session, task_cfg.host),
            }

        if command == "restart":
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            result = cli.tmux.restart_task(task_name)
            return {"command": command, "session": session, "result": result}

        if command == "kill":
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            result = cli.tmux.kill_task(task_name)
            return {"command": command, "session": session, "result": result}

        if command == "logs":
            task_name = params.get("task")
            lines = params.get("lines", 100)
            if not task_name:
                return {"error": "missing_task", "session": session}
            try:
                if not cli.tmux.session_exists():
                    return {"error": "session_not_running", "session": session}
                sess = cli.tmux._get_session()
                window = sess.windows.get(window_name=task_name, default=None)
                if window and window.active_pane:
                    output = window.active_pane.cmd("capture-pane", "-p", "-S", f"-{lines}").stdout
                    return {"command": command, "session": session, "logs": output}
            except Exception as e:  # noqa: BLE001
                return {"error": str(e), "session": session}
            return {"error": "could_not_retrieve_logs", "session": session}

        return {"error": "unknown_command", "command": command}

    # ---- status helpers ----

    def _project_status(self, cli: TaskmuxCLI) -> dict:
        session_exists = cli.tmux.session_exists()
        tasks: dict[str, dict] = {}
        for task_name in cli.config.tasks:
            tasks[task_name] = cli.tmux.get_task_status(task_name)
        return {
            "session_name": cli.config.name,
            "session_exists": session_exists,
            "tasks": tasks,
            "config_path": str(cli.config_path),
            "timestamp": datetime.now().isoformat(),
        }

    async def _aggregate_status(self) -> dict:
        async with self._lock:
            sessions = self._all_known_sessions_locked()
            loaded = dict(self.projects)
            states = dict(self.project_states)
            paths = dict(self.project_paths)
        out_projects = []
        for session in sessions:
            cli = loaded.get(session)
            state = states.get(session, "ok" if cli else "config_missing")
            if cli is None:
                out_projects.append(
                    {
                        "session": session,
                        "state": state,
                        "config_path": paths.get(session, ""),
                    }
                )
                continue
            try:
                out_projects.append(
                    {"session": session, "state": state} | self._project_status(cli)
                )
            except Exception as e:  # noqa: BLE001
                out_projects.append({"session": session, "state": "error", "error": str(e)})
        return {
            "projects": out_projects,
            "count": len(out_projects),
            "timestamp": datetime.now().isoformat(),
        }

    async def _list_projects(self) -> list[dict]:
        async with self._lock:
            sessions = self._all_known_sessions_locked()
            loaded = dict(self.projects)
            states = dict(self.project_states)
            paths = dict(self.project_paths)
        out: list[dict] = []
        for session in sessions:
            cli = loaded.get(session)
            state = states.get(session, "ok" if cli else "config_missing")
            row: dict = {
                "session": session,
                "config_path": str(cli.config_path) if cli else paths.get(session, ""),
                "state": state,
            }
            if cli is not None:
                row["session_exists"] = cli.tmux.session_exists()
                row["task_count"] = len(cli.config.tasks)
            else:
                row["session_exists"] = False
                row["task_count"] = 0
            out.append(row)
        return out

    def _all_known_sessions_locked(self) -> list[str]:
        """Union of registry entries + currently loaded projects, sorted."""
        on_disk = readRegistry()
        return sorted(set(on_disk.keys()) | set(self.projects.keys()))

    async def _broadcast_to_clients(self, message: dict) -> None:
        if not self.websocket_clients:
            return
        payload = json.dumps(message)
        disconnected = set()
        for client in self.websocket_clients:
            try:
                await client.send(payload)
            except Exception:  # noqa: BLE001
                disconnected.add(client)
        self.websocket_clients -= disconnected


# ---------------------------------------------------------------------------
# Non-daemon helpers
# ---------------------------------------------------------------------------


class SimpleConfigWatcher:
    """Simple config file watcher for `taskmux watch` (non-daemon mode)."""

    def __init__(self, taskmux_cli: TaskmuxCLI):
        self.taskmux_cli = taskmux_cli

    def watch_config(self) -> None:
        print("Watching taskmux.toml for changes...")
        print("Press Ctrl+C to stop")

        loop = asyncio.new_event_loop()
        observer = Observer()
        observer.schedule(
            ConfigWatcher(self.taskmux_cli, loop),
            str(self.taskmux_cli.config_path.parent),
            recursive=False,
        )
        observer.start()

        try:
            while True:
                # Drain any pending callbacks scheduled by watcher events.
                loop.call_soon(loop.stop)
                loop.run_forever()
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            print("\nStopped watching")

        observer.join()
        loop.close()


def list_running_projects() -> list[dict]:
    """Return the registry contents annotated with daemon status.

    Used by `taskmux daemon list` when querying without a live daemon.
    """
    from .registry import listRegistered

    out: list[dict] = []
    for entry in listRegistered():
        out.append(
            {
                "session": entry["session"],
                "config_path": entry["config_path"],
                "registered_at": entry["registered_at"],
            }
        )
    return out


# Backwards-compat shim — referenced by older code paths during the transition.
def list_running_daemons() -> list[dict]:
    """Deprecated: returns the registered project list (single global daemon now)."""
    return list_running_projects()
