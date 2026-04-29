"""Unified multi-project daemon for Taskmux.

A single daemon process per host owns all task processes for every registered
project. State is keyed by session name:
  - `self.projects[session]` -> Supervisor (PTY-backed process owner)
  - `self.configs[session]`  -> parsed TaskmuxConfig
  - `self.config_paths[session]` -> abs path to taskmux.toml

Reads the registry at ~/.taskmux/registry.json + watches each project's
taskmux.toml for live reloads. Auto-restart loop calls supervisor.auto_restart_tasks
per project. WebSocket API fans out lifecycle commands to the right supervisor.
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
from datetime import datetime
from pathlib import Path

import websockets
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import addTask, loadConfig, removeTask
from .errors import TaskmuxError
from .events import recordEvent
from .host_resolver import HostResolver, getResolver
from .models import TaskmuxConfig
from .paths import (
    REGISTRY_PATH,
    ensureTaskmuxDir,
    globalDaemonLogPath,
    globalDaemonPidPath,
    projectLogsDir,
)
from .proxy import ProxyServer
from .registry import readRegistry
from .supervisor import Supervisor, _parseSince, make_supervisor, readLogFile
from .tunnels import (
    CloudflareTunnelBackend,
    NoopTunnelBackend,
    TunnelBackend,
    TunnelMapping,
)
from .url import taskUrl

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
    """Watches one project's taskmux.toml; calls back on change/missing."""

    def __init__(
        self,
        session: str,
        config_path: Path,
        loop: asyncio.AbstractEventLoop,
        on_reload: callable | None = None,  # type: ignore[type-arg]
        on_missing: callable | None = None,  # type: ignore[type-arg]
    ):
        self.session = session
        self.target_path = str(config_path)
        self.loop = loop
        self.on_reload = on_reload
        self.on_missing = on_missing
        self.logger = logging.getLogger("taskmux-daemon")

    def _matches(self, event: FileSystemEvent) -> bool:
        if str(event.src_path) == self.target_path:
            return True
        dest = getattr(event, "dest_path", None)
        return dest is not None and str(dest) == self.target_path

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self.loop.call_soon_threadsafe(self._fire_reload)

    def on_created(self, event: FileSystemEvent) -> None:
        if self._matches(event):
            self.loop.call_soon_threadsafe(self._fire_reload)

    def on_moved(self, event: FileSystemEvent) -> None:
        dest = getattr(event, "dest_path", None)
        if dest is not None and str(dest) == self.target_path:
            self.loop.call_soon_threadsafe(self._fire_reload)
        elif str(event.src_path) == self.target_path:
            self.loop.call_soon_threadsafe(self._fire_missing)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if str(event.src_path) == self.target_path:
            self.loop.call_soon_threadsafe(self._fire_missing)

    def _fire_reload(self) -> None:
        if self.on_reload:
            self.on_reload(self.session)

    def _fire_missing(self) -> None:
        if self.on_missing:
            self.on_missing(self.session)


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
    """Unified multi-project daemon — owns all task processes via Supervisor."""

    def __init__(self, api_port: int | None = None):
        from .global_config import loadGlobalConfig

        self.global_config = loadGlobalConfig()
        self.api_port = api_port if api_port is not None else self.global_config.api_port
        self.running = False
        self.health_check_interval = self.global_config.health_check_interval
        self.health_check_task: asyncio.Task | None = None
        self.websocket_clients: set = set()
        # session -> Supervisor / TaskmuxConfig / abs-path / state
        self.projects: dict[str, Supervisor] = {}
        self.configs: dict[str, TaskmuxConfig] = {}
        self.config_paths: dict[str, Path] = {}
        self.project_states: dict[str, str] = {}
        self.observers: dict[str, Observer] = {}  # type: ignore[reportInvalidTypeForm]
        self.registry_observer: Observer | None = None  # type: ignore[reportInvalidTypeForm]
        self.proxy: ProxyServer | None = None
        self._proxy_eligible = False
        self._proxy_started = False
        self._proxy_sock: socket.socket | None = None
        self.host_resolver: HostResolver | None = None
        self.dns_server: object | None = None
        # (project_id, backend_name) -> TunnelBackend. Lazily created on
        # first sync for projects that opt in via `tunnel = "..."` on a task.
        self.tunnels: dict[tuple[str, str], TunnelBackend] = {}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.logger = self._setup_logging()

    # ---- logging ----

    def _setup_logging(self) -> logging.Logger:
        logger = logging.getLogger("taskmux-daemon")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()

        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

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

    # ---- lifecycle ----

    async def start(self) -> None:
        existing = get_daemon_pid()
        if existing is not None and existing != os.getpid():
            self.logger.error(f"Global daemon already running (pid {existing})")
            sys.exit(1)

        self._warn_if_unprivileged()
        self._proxy_sock = self._pre_bind_proxy_socket()
        self._install_resolver_root()
        self._drop_privileges()

        _write_daemon_pid()
        self.running = True
        self._loop = asyncio.get_running_loop()

        # Use the loop's own signal handlers — async-safe, can schedule shutdown.
        with contextlib.suppress(NotImplementedError):
            self._loop.add_signal_handler(
                signal.SIGTERM, lambda: asyncio.create_task(self._async_shutdown("SIGTERM"))
            )
            self._loop.add_signal_handler(
                signal.SIGINT, lambda: asyncio.create_task(self._async_shutdown("SIGINT"))
            )

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

    async def _async_shutdown(self, reason: str) -> None:
        """Stop all task processes (with grace), then drop the daemon."""
        self.logger.info(f"{reason} received — stopping all task processes")
        self.running = False
        # Snapshot then stop_all on each supervisor so process trees die
        # within stop_grace_period rather than orphaning.
        async with self._lock:
            sessions = list(self.projects.items())
        for session, sup in sessions:
            try:
                if sup.session_exists():
                    await sup.stop_all()
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"stop_all failed for {session}: {e}")
        for (session, kind), backend in list(self.tunnels.items()):
            try:
                await backend.clear()
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"tunnel clear failed for {session}/{kind}: {e}")
        self.stop()
        # Cancel the long-running tasks so start()'s gather() unblocks.
        if self.health_check_task and not self.health_check_task.done():
            self.health_check_task.cancel()
        # Schedule final exit on next loop tick.
        if self._loop is not None:
            self._loop.call_soon(sys.exit, 0)

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
        async with self._lock:
            on_disk = readRegistry()
            # Treat config_missing/error as "needs retry" rather than "current",
            # so a recreated taskmux.toml can re-register on the next sync.
            healthy = {s for s in self.projects if self.project_states.get(s) == "ok"}
            known = set(self.projects.keys()) | set(self.config_paths.keys())
            wanted = set(on_disk.keys())

            for session in wanted - healthy:
                entry = on_disk[session]
                self._register_locked(session, Path(entry["config_path"]))

            for session in known - wanted:
                self._unregister_locked(session)

    def _register_locked(self, session: str, config_path: Path) -> None:
        """Load config + create Supervisor. Caller holds self._lock.

        `session` is the worktree-aware project_id stored in the registry.
        We compose project_id locally too and verify they match — mismatch
        means the registry is stale; we use the registry key.
        """
        from .config import loadProjectIdentity

        self.config_paths[session] = config_path

        if session in self.projects:
            return
        if not config_path.exists():
            self.logger.warning(f"Registry entry for '{session}' points to missing {config_path}")
            self.project_states[session] = "config_missing"
            return
        try:
            identity = loadProjectIdentity(config_path)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Failed to load '{session}' from {config_path}: {e}")
            self.project_states[session] = "error"
            return

        cfg = identity.config
        if identity.project_id != session:
            self.logger.warning(
                f"Registry session '{session}' != project_id '{identity.project_id}' "
                f"for {config_path}; using registry key"
            )

        sup = make_supervisor(
            cfg,
            config_dir=config_path.parent,
            project_id=session,
            worktree_id=identity.worktree_id,
        )
        sup.on_task_route_change = self._on_task_route_change

        self.configs[session] = cfg
        self.projects[session] = sup
        self.project_states[session] = "ok"

        if self._proxy_eligible and self.proxy is not None:
            self._mint_and_register_proxy(session)
            if self._loop is not None and not self._proxy_started:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self._start_proxy_listener())
                )
        if self.host_resolver is not None:
            self._sync_hostnames()
            tld = self.global_config.dns_managed_tld
            new_hosts: list[str] = []
            for tc in cfg.tasks.values():
                if tc.host is None or tc.host == "*":
                    continue
                if tc.host == "":
                    new_hosts.append(f"{session}.{tld}")
                else:
                    new_hosts.append(f"{tc.host}.{session}.{tld}")
            if new_hosts and self.host_resolver.name == "etc_hosts":
                self.logger.info(
                    f"Project '{session}' added with hosts {new_hosts}. "
                    f"Restart `sudo taskmux daemon` to refresh /etc/hosts "
                    f'(or use host_resolver = "dns_server" for dynamic adds).'
                )

        if self._loop is not None:
            observer = Observer()
            handler = ConfigWatcher(
                session,
                config_path,
                self._loop,
                on_reload=lambda s: self._on_config_reload(s),
                on_missing=lambda s: asyncio.create_task(self._mark_missing(s)),
            )
            observer.schedule(handler, str(config_path.parent), recursive=False)
            observer.start()
            self.observers[session] = observer

        self.logger.info(f"Registered project '{session}' from {config_path}")

    def _unregister_locked(self, session: str) -> None:
        observer = self.observers.pop(session, None)
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.stop()
                observer.join(timeout=2)
        self.projects.pop(session, None)
        self.configs.pop(session, None)
        self.project_states.pop(session, None)
        self.config_paths.pop(session, None)
        for sess, kind in list(self.tunnels.keys()):
            if sess != session:
                continue
            backend = self.tunnels.pop((sess, kind))
            if self._loop is not None:
                self._loop.call_soon(lambda b=backend: asyncio.create_task(b.clear()))
        if self.proxy is not None:
            self.proxy.unregister_project(session)
            from .ca import dropCert

            with contextlib.suppress(Exception):
                dropCert(session)
        self._sync_hostnames()
        self.logger.info(f"Unregistered project '{session}'")

    # ---- privileged bootstrap ----

    def _warn_if_unprivileged(self) -> None:
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            return
        if not self.global_config.proxy_enabled:
            return
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
        if os.environ.get("TASKMUX_DISABLE_PROXY") == "1":
            return
        if not self.global_config.proxy_enabled:
            return

        kind = self.global_config.host_resolver
        if kind == "etc_hosts":
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
        """Walk the registry; emit (fqdn, 127.0.0.1) for each task host.

        Apex (`""`) emits `{project_id}.{tld}`. Wildcard (`"*"`) is skipped
        for etc_hosts (no glob support); the `dns_server` resolver answers
        wildcards in code.
        """
        from .aliases import loadAliases
        from .config import loadProjectIdentity

        mappings: list[tuple[str, str]] = []
        wildcard_under_etc_hosts: list[str] = []
        tld = self.global_config.dns_managed_tld
        for session, entry in readRegistry().items():
            cfg_path = Path(entry["config_path"])
            if not cfg_path.exists():
                continue
            try:
                identity = loadProjectIdentity(cfg_path)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"Skipping host collection for {session!r}: {e}")
                continue
            project_id = session if session == identity.project_id else identity.project_id
            for task_cfg in identity.config.tasks.values():
                if task_cfg.host is None:
                    continue
                if task_cfg.host == "*":
                    wildcard_under_etc_hosts.append(project_id)
                    continue
                if task_cfg.host == "":
                    fqdn = f"{project_id}.{tld}"
                else:
                    fqdn = f"{task_cfg.host}.{project_id}.{tld}"
                mappings.append((fqdn, "127.0.0.1"))
            for alias_entry in loadAliases(identity.project, identity.worktree_id).values():
                host = alias_entry["host"]
                fqdn = f"{project_id}.{tld}" if host == "" else f"{host}.{project_id}.{tld}"
                mappings.append((fqdn, "127.0.0.1"))
        if wildcard_under_etc_hosts and self.global_config.host_resolver == "etc_hosts":
            projects = ", ".join(sorted(set(wildcard_under_etc_hosts)))
            self.logger.warning(
                f"Wildcard hosts on {projects} won't resolve under host_resolver='etc_hosts' "
                f"(no wildcard support in /etc/hosts). Switch to host_resolver='dns_server' "
                f"in ~/.taskmux/config.toml so unmapped subdomains catch-all to 127.0.0.1."
            )
        return mappings

    def _sync_hostnames(self) -> None:
        if self.host_resolver is None:
            return
        mappings = self._collect_host_mappings()
        with contextlib.suppress(PermissionError, OSError):
            self.host_resolver.sync(mappings)

    def _collect_tunnel_mappings(self, session: str) -> dict[str, list[TunnelMapping]]:
        """Per-backend tunnel mappings for one project.

        Walks the project's live tasks; for each task with `tunnel` set, emits
        `(public_hostname, internal_fqdn, proxy_port)`. Apex hosts compose to
        `<project_id>.<tld>`; wildcard hosts are skipped (no single FQDN to
        target). Tasks without `host` or without `public_hostname` are
        likewise skipped (caught earlier by config validation, but defensive).
        """
        cfg = self.configs.get(session)
        sup = self.projects.get(session)
        if cfg is None or sup is None:
            return {}
        tld = self.global_config.dns_managed_tld
        proxy_port = self.global_config.proxy_https_port
        running: set[str] = set()
        with contextlib.suppress(Exception):
            if sup.session_exists():
                running = set(sup.list_windows())

        out: dict[str, list[TunnelMapping]] = {}
        for task_name, task_cfg in cfg.tasks.items():
            if task_cfg.tunnel is None or not task_cfg.public_hostname:
                continue
            if task_cfg.host is None or task_cfg.host == "*":
                continue
            if task_name not in running:
                # Tunnel only what the proxy is currently routing.
                continue
            internal_fqdn = (
                f"{session}.{tld}" if task_cfg.host == "" else f"{task_cfg.host}.{session}.{tld}"
            )
            out.setdefault(str(task_cfg.tunnel), []).append(
                (task_cfg.public_hostname, internal_fqdn, proxy_port)
            )
        return out

    def _ensure_tunnel_backend(self, session: str, kind: str) -> TunnelBackend | None:
        """Get-or-create the backend instance for (project, kind)."""
        existing = self.tunnels.get((session, kind))
        if existing is not None:
            return existing
        cfg = self.configs.get(session)
        if cfg is None:
            return None
        backend: TunnelBackend | None = None
        if kind == "noop":
            backend = NoopTunnelBackend()
        elif kind == "cloudflare":
            backend = self._build_cloudflare_backend(session, cfg)
        if backend is None:
            return None
        self.tunnels[(session, kind)] = backend
        return backend

    def _build_cloudflare_backend(self, session: str, cfg: TaskmuxConfig) -> TunnelBackend | None:
        from .global_config import globalConfigModeOk
        from .tunnels import resolveCloudflareConfig

        # Refuse to read an embedded token from a world-readable config.
        if self.global_config.tunnel.cloudflare.api_token:
            ok, mode = globalConfigModeOk()
            if not ok:
                mode_str = oct(mode) if mode is not None else "?"
                self.logger.error(
                    f"~/.taskmux/config.toml is mode {mode_str} but contains "
                    "[tunnel.cloudflare].api_token — refusing to read. "
                    "Run `chmod 600 ~/.taskmux/config.toml` (or move the token "
                    "to an env var via api_token_env). Tunnel disabled."
                )
                return None

        eff = resolveCloudflareConfig(
            global_cf=self.global_config.tunnel.cloudflare,
            project_cf=cfg.tunnel.cloudflare,
            project_id=session,
        )
        missing: list[str] = []
        if not eff.account_id:
            missing.append(
                "account_id (set [tunnel.cloudflare].account_id in ~/.taskmux/config.toml)"
            )
        if not eff.api_token:
            env_name = self.global_config.tunnel.cloudflare.api_token_env or "CLOUDFLARE_API_TOKEN"
            missing.append(f"api_token (embed in ~/.taskmux/config.toml or export ${env_name})")
        if not eff.zone_id:
            # Auto-resolution requires an API call — defer to the wizard /
            # `taskmux tunnel enable`. Daemon does NOT auto-resolve to avoid
            # masking a config error with a quiet network round-trip.
            missing.append(
                "zone_id (set [tunnel.cloudflare].zone_id, or run "
                "`taskmux tunnel enable` to auto-resolve from public_hostname)"
            )
        if missing:
            self.logger.error(
                f"Project '{session}' tunnel='cloudflare' disabled — missing: "
                + "; ".join(missing)
                + ". Tasks still serve locally."
            )
            return None
        return CloudflareTunnelBackend(
            account_id=eff.account_id,  # type: ignore[arg-type]
            api_token=eff.api_token,  # type: ignore[arg-type]
            zone_id=eff.zone_id,  # type: ignore[arg-type]
            tunnel_name=eff.tunnel_name,
            proxy_port=self.global_config.proxy_https_port,
        )

    async def _sync_tunnels(self, session: str) -> None:
        """Reconcile every configured tunnel backend for one project.

        Empty mapping list ⇒ tear that backend down (clear), but keep the
        instance + cached state so the next sync is cheap.
        """
        per_kind = self._collect_tunnel_mappings(session)
        cfg = self.configs.get(session)
        configured_kinds: set[str] = set()
        if cfg is not None:
            for tc in cfg.tasks.values():
                if tc.tunnel is not None:
                    configured_kinds.add(str(tc.tunnel))
        for kind in configured_kinds:
            backend = self._ensure_tunnel_backend(session, kind)
            if backend is None:
                continue
            mappings = per_kind.get(kind, [])
            try:
                if mappings:
                    await backend.sync(mappings)
                else:
                    await backend.clear()
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"Tunnel '{kind}' sync for {session!r} failed: {e}")

    def _drop_privileges(self) -> None:
        """If running as root via sudo, drop to SUDO_UID/SUDO_GID."""
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            return
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if not sudo_uid or not sudo_gid:
            self.logger.warning(
                "Daemon is running as root with no SUDO_UID — staying as root. "
                "state/cert ownership will be off; prefer `sudo taskmux daemon`."
            )
            return
        import pwd as _pwd

        uid = int(sudo_uid)
        gid = int(sudo_gid)
        pw = _pwd.getpwuid(uid)
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
        os.environ["HOME"] = pw.pw_dir
        os.environ["USER"] = pw.pw_name
        os.environ["LOGNAME"] = pw.pw_name
        from . import paths as _paths

        _paths.TASKMUX_DIR = Path(pw.pw_dir) / ".taskmux"
        _paths.EVENTS_FILE = _paths.TASKMUX_DIR / "events.jsonl"
        _paths.PROJECTS_DIR = _paths.TASKMUX_DIR / "projects"
        _paths.CERTS_DIR = _paths.TASKMUX_DIR / "certs"
        _paths.REGISTRY_PATH = _paths.TASKMUX_DIR / "registry.json"
        _paths.GLOBAL_DAEMON_PID = _paths.TASKMUX_DIR / "daemon.pid"
        _paths.GLOBAL_DAEMON_LOG = _paths.TASKMUX_DIR / "daemon.log"
        _paths.GLOBAL_CONFIG_PATH = _paths.TASKMUX_DIR / "config.toml"
        global REGISTRY_PATH
        REGISTRY_PATH = _paths.REGISTRY_PATH
        self.logger.info(f"Dropped privileges: now running as {pw.pw_name} (uid={uid}, gid={gid})")

    # ---- proxy ----

    async def _maybe_start_proxy(self) -> None:
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
        async with self._lock:
            for session in list(self.projects.keys()):
                self._mint_and_register_proxy(session)
            await self._start_proxy_listener()

    async def _start_proxy_listener(self) -> None:
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

    def _mint_and_register_proxy(self, session: str) -> None:
        if self.proxy is None:
            return
        cfg = self.configs.get(session)
        sup = self.projects.get(session)
        if cfg is None or sup is None:
            return
        from .ca import mintCert

        try:
            cert, key = mintCert(session)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"mkcert failed for '{session}': {e}")
            return
        self.proxy.register_project(session, cert, key)
        running = set(sup.list_windows()) if sup.session_exists() else set()
        for task_name, task_cfg in cfg.tasks.items():
            if task_cfg.host is None:
                continue
            port = sup.assigned_ports.get(task_name)
            if port is not None and task_name in running:
                self.proxy.set_route(session, task_cfg.host, port)
        from .aliases import loadAliases as _loadAliases

        for alias_entry in _loadAliases(sup.config.name, sup.worktree_id).values():
            self.proxy.set_route(session, alias_entry["host"], alias_entry["port"])

    def _on_task_route_change(self, project: str, _task: str, host: str, port: int | None) -> None:
        if self.proxy is None:
            return
        if port is None:
            self.proxy.drop_route(project, host)
        else:
            self.proxy.set_route(project, host, port)

    async def _resync_project_routes(self, session: str) -> dict:
        """Reconcile a project's proxy routes against disk state + live tasks.

        Used after an out-of-band CLI lifecycle command (start/stop/restart/kill
        or alias add/remove) in a separate process. We re-read assigned_ports
        from state.json, build the desired host set from (live tasks ∪ aliases),
        drop any existing route not in that set, and set the desired ones.
        """
        from .aliases import loadAliases as _loadAliases

        async with self._lock:
            sup = self.projects.get(session)
            cfg = self.configs.get(session)
        if sup is None or cfg is None:
            return {"ok": False, "error": "unknown_session", "added": [], "dropped": []}
        sup.reload_state()

        added: list[str] = []
        dropped: list[str] = []
        running_windows: set[str] = set()
        try:
            if sup.session_exists():
                running_windows = set(sup.list_windows())
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"resync: list_windows failed for {session!r}: {e}")

        desired: dict[str, int] = {}
        for task_name, task_cfg in cfg.tasks.items():
            if task_cfg.host is None:
                continue
            port = sup.assigned_ports.get(task_name)
            if task_name in running_windows and port is not None:
                desired[task_cfg.host] = port
        for alias_entry in _loadAliases(sup.config.name, sup.worktree_id).values():
            desired[alias_entry["host"]] = alias_entry["port"]

        if self.proxy is not None:
            snapshot = (
                self.proxy.routes_snapshot() if hasattr(self.proxy, "routes_snapshot") else {}
            )
            existing = set(snapshot.get(session, {}).keys())
            for host in existing - desired.keys():
                self.proxy.drop_route(session, host)
                dropped.append(host)
            for host, port in desired.items():
                self.proxy.set_route(session, host, port)
                added.append(host)
        else:
            added.extend(desired.keys())
        with contextlib.suppress(Exception):
            self._sync_hostnames()
        with contextlib.suppress(Exception):
            await self._sync_tunnels(session)
        return {"ok": True, "added": added, "dropped": dropped}

    async def _mark_missing(self, session: str) -> None:
        async with self._lock:
            observer = self.observers.pop(session, None)
            if observer is not None:
                with contextlib.suppress(Exception):
                    observer.stop()
                    observer.join(timeout=2)
            self.projects.pop(session, None)
            self.configs.pop(session, None)
            self.project_states[session] = "config_missing"
            if self.proxy is not None:
                self.proxy.unregister_project(session)
                from .ca import dropCert

                with contextlib.suppress(Exception):
                    dropCert(session)
        self._sync_hostnames()
        self.logger.info(f"Project '{session}' marked config_missing — health checks paused")

    def _on_config_reload(self, session: str) -> None:
        """ConfigWatcher reload — reload the parsed config + refresh routes."""
        if self._loop is None:
            return

        async def _do() -> None:
            cfg_path = self.config_paths.get(session)
            if cfg_path is None or not cfg_path.exists():
                return
            try:
                cfg = loadConfig(cfg_path)
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"Config reload failed for {session}: {e}")
                return
            sup = self.projects.get(session)
            if sup is not None:
                sup.config = cfg
            self.configs[session] = cfg
            recordEvent("config_reloaded", session=session)
            self.logger.info(f"Reloaded config for '{session}'")
            await self._resync_project_routes(session)
            self._sync_hostnames()

        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(_do()))

    # ---- health loop ----

    async def _health_check_loop(self) -> None:
        while self.running:
            try:
                async with self._lock:
                    snapshot = list(self.projects.items())
                for session, sup in snapshot:
                    try:
                        # No session_exists guard: auto_restart_tasks must
                        # also revive tasks whose process already exited
                        # (otherwise they stay dead forever).
                        await sup.auto_restart_tasks()
                    except Exception as e:  # noqa: BLE001
                        self.logger.error(f"Health check error for '{session}': {e}")

                if self.websocket_clients:
                    payload = await self._aggregate_status()
                    await self._broadcast_to_clients({"type": "health_check", "data": payload})

                await asyncio.sleep(self.health_check_interval)
            except asyncio.CancelledError:
                raise
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
                        await websocket.send(json.dumps(response, default=str))
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
            "start",
            "stop",
            "restart",
            "kill",
            "start_all",
            "stop_all",
            "restart_all",
            "inspect",
            "health",
            "list_tasks",
            "events",
            "logs",
            "logs_clean",
            "add_task",
            "remove_task",
            "proxy_routes",
            "tunnel_status",
            "tunnel_config_get",
            "tunnel_config_set",
            "tunnel_test",
            "tunnel_enable",
            "tunnel_disable",
            "url",
            "resync",
            "sync_registry",
            "ping",
        }
    )

    async def _handle_api_request(self, data: dict) -> dict:
        command = data.get("command")
        params = data.get("params", {}) or {}

        if command not in self.KNOWN_COMMANDS:
            return {"error": "unknown_command", "command": command}

        if command == "ping":
            return {"command": command, "ok": True}

        if command == "sync_registry":
            await self._sync_with_registry()
            return {"command": command, "ok": True, "count": len(self.projects)}

        if command == "list_projects":
            return {"command": command, "projects": await self._list_projects()}

        if command == "status_all":
            return {"command": command, "data": await self._aggregate_status()}

        if command == "proxy_routes":
            routes = self.proxy.routes_snapshot() if self.proxy is not None else {}
            return {"command": command, "running": self.proxy is not None, "routes": routes}

        if command == "tunnel_status":
            entries = []
            for (sess, _kind), backend in sorted(self.tunnels.items()):
                snap = backend.status()
                snap["session"] = sess
                entries.append(snap)
            return {"command": command, "tunnels": entries}

        if command == "tunnel_config_get":
            from .tunnel_wizard import describeTunnelConfig

            session_param = params.get("session")
            cfg_path = self.config_paths.get(session_param) if session_param else None
            if cfg_path is None:
                return {
                    "error": "missing_session",
                    "command": command,
                    "hint": "tunnel_config_get requires params.session",
                }
            payload = describeTunnelConfig(config_path=cfg_path, reveal=bool(params.get("reveal")))
            return {"command": command, **payload}

        if command == "tunnel_config_set":
            from .tunnel_wizard import setTunnelConfig

            scope = params.get("scope", "global")
            updates = params.get("updates") or {}
            session_param = params.get("session")
            cfg_path = self.config_paths.get(session_param) if session_param else None
            try:
                payload = setTunnelConfig(scope=scope, updates=updates, config_path=cfg_path)
            except TaskmuxError as e:
                return {"command": command, **e.to_dict()}
            return {"command": command, **payload}

        if command == "tunnel_test":
            from .global_config import loadGlobalConfig as _load_global
            from .tunnel_wizard import preflight as _preflight

            session_param = params.get("session")
            if not session_param or session_param not in self.configs:
                return {"error": "missing_session", "command": command}
            report = await _preflight(
                project_id=session_param,
                project_cfg=self.configs[session_param],
                global_cfg=_load_global(),
            )
            return {"command": command, "ok": report.ok, "preflight": report.to_dict()}

        if command == "tunnel_enable":
            from .tunnel_wizard import enable as _enable

            session_param = params.get("session")
            cfg_path = self.config_paths.get(session_param) if session_param else None
            if cfg_path is None:
                return {"error": "missing_session", "command": command}
            try:
                result = await _enable(
                    config_path=cfg_path,
                    api_token=params.get("api_token"),
                    account_id=params.get("account_id"),
                    zone_id=params.get("zone_id"),
                    tasks=params.get("tasks"),
                    public_hostnames=params.get("public_hostnames") or {},
                    dry_run=bool(params.get("dry_run")),
                )
            except TaskmuxError as e:
                return {"command": command, **e.to_dict()}
            return {"command": command, **result.to_dict()}

        if command == "tunnel_disable":
            from .tunnel_wizard import disable as _disable

            session_param = params.get("session")
            cfg_path = self.config_paths.get(session_param) if session_param else None
            if cfg_path is None:
                return {"error": "missing_session", "command": command}
            payload = await _disable(config_path=cfg_path, prune=bool(params.get("prune")))
            return {"command": command, **payload}

        # Session-scoped commands
        session = params.get("session")
        if not session:
            return {"error": "missing_session", "command": command}
        async with self._lock:
            sup = self.projects.get(session)
            cfg = self.configs.get(session)
        if sup is None and command == "resync":
            # CLI may have just registered the project (e.g. `alias add` on a
            # fresh project). Beat the watcher: sync once, then look again.
            await self._sync_with_registry()
            async with self._lock:
                sup = self.projects.get(session)
                cfg = self.configs.get(session)
        if sup is None or cfg is None:
            return {"error": "unknown_session", "session": session, "command": command}

        if command == "status":
            return {"command": command, "session": session, "data": self._project_status(session)}

        if command == "list_tasks":
            return {"command": command, "session": session, "data": sup.list_tasks()}

        if command == "resync":
            return {
                "command": command,
                "session": session,
                "data": await self._resync_project_routes(session),
            }

        if command == "url":
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            task_cfg = cfg.tasks.get(task_name)
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

        if command in ("start", "stop", "restart", "kill", "inspect", "health"):
            task_name = params.get("task")
            if not task_name:
                return {"error": "missing_task", "session": session}
            if command == "start":
                result = await sup.start_task(task_name)
            elif command == "stop":
                result = await sup.stop_task(task_name)
            elif command == "restart":
                result = await sup.restart_task(task_name)
            elif command == "kill":
                result = await sup.kill_task(task_name)
            elif command == "inspect":
                result = sup.inspect_task(task_name)
            else:  # health
                hr = sup.check_health(task_name)
                result = {"ok": True, "task": task_name, **hr.to_dict()}
            return {"command": command, "session": session, "result": result}

        if command == "start_all":
            return {"command": command, "session": session, "result": await sup.start_all()}
        if command == "stop_all":
            return {"command": command, "session": session, "result": await sup.stop_all()}
        if command == "restart_all":
            return {"command": command, "session": session, "result": await sup.restart_all()}

        if command == "events":
            from .events import queryEvents

            task = params.get("task")
            since = params.get("since")
            limit = params.get("limit", 50)
            since_dt = _parseSince(since) if since else None
            return {
                "command": command,
                "session": session,
                "events": queryEvents(task=task, session=session, since=since_dt, limit=limit),
            }

        if command == "logs":
            task = params.get("task")
            lines = params.get("lines", 100)
            grep = params.get("grep")
            since = params.get("since")
            if task:
                log_path = sup.getLogPath(task)
                out = readLogFile(log_path, lines, grep, since) if log_path else []
                return {"command": command, "session": session, "task": task, "lines": out}
            tasks_logs: dict[str, list[str]] = {}
            for name in cfg.tasks:
                lp = sup.getLogPath(name)
                tasks_logs[name] = readLogFile(lp, lines, grep, since) if lp else []
            return {"command": command, "session": session, "tasks": tasks_logs}

        if command == "logs_clean":
            task = params.get("task")
            log_dir = projectLogsDir(cfg.name, sup.worktree_id)
            if not log_dir.exists():
                return {"command": command, "session": session, "deleted": 0}
            if task:
                count = 0
                for f in log_dir.glob(f"{task}.log*"):
                    f.unlink()
                    count += 1
                return {"command": command, "session": session, "task": task, "deleted": count}
            import shutil

            shutil.rmtree(log_dir)
            return {"command": command, "session": session, "action": "logs_cleaned"}

        if command == "add_task":
            task = params.get("task")
            cmd = params.get("command")
            if not task or not cmd:
                return {"error": "missing_task_or_command", "session": session}
            cfg_path = self.config_paths.get(session)
            addTask(
                cfg_path,
                task,
                cmd,
                cwd=params.get("cwd"),
                host=params.get("host"),
                health_check=params.get("health_check"),
                depends_on=params.get("depends_on"),
            )
            return {"command": command, "session": session, "task": task, "action": "added"}

        if command == "remove_task":
            task = params.get("task")
            if not task:
                return {"error": "missing_task", "session": session}
            cfg_path = self.config_paths.get(session)
            if task in sup.list_windows():
                await sup.kill_task(task)
            _, removed = removeTask(cfg_path, task)
            return {
                "command": command,
                "session": session,
                "task": task,
                "removed": removed,
                "action": "removed",
            }

        return {"error": "unknown_command", "command": command}

    # ---- status helpers ----

    def _project_status(self, session: str) -> dict:
        sup = self.projects.get(session)
        cfg = self.configs.get(session)
        cfg_path = self.config_paths.get(session)
        if sup is None or cfg is None:
            return {
                "session_name": session,
                "session_exists": False,
                "tasks": {},
                "config_path": str(cfg_path) if cfg_path else "",
                "timestamp": datetime.now().isoformat(),
            }
        return {
            "session_name": cfg.name,
            "session_exists": sup.session_exists(),
            "tasks": {n: sup.get_task_status(n) for n in cfg.tasks},
            "config_path": str(cfg_path) if cfg_path else "",
            "timestamp": datetime.now().isoformat(),
        }

    async def _aggregate_status(self) -> dict:
        async with self._lock:
            sessions = self._all_known_sessions_locked()
            states = dict(self.project_states)
            paths = dict(self.config_paths)
            loaded = set(self.projects.keys())
        out_projects = []
        for session in sessions:
            state = states.get(session, "ok" if session in loaded else "config_missing")
            if session not in loaded:
                out_projects.append(
                    {
                        "session": session,
                        "state": state,
                        "config_path": str(paths.get(session, "")),
                    }
                )
                continue
            try:
                out_projects.append(
                    {"session": session, "state": state} | self._project_status(session)
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
            states = dict(self.project_states)
            paths = dict(self.config_paths)
            loaded = dict(self.projects)
            configs = dict(self.configs)
        out: list[dict] = []
        for session in sessions:
            sup = loaded.get(session)
            cfg = configs.get(session)
            state = states.get(session, "ok" if sup else "config_missing")
            cfg_path = paths.get(session)
            row: dict = {
                "session": session,
                "config_path": str(cfg_path) if cfg_path else "",
                "state": state,
            }
            if sup is not None and cfg is not None:
                row["session_exists"] = sup.session_exists()
                row["task_count"] = len(cfg.tasks)
            else:
                row["session_exists"] = False
                row["task_count"] = 0
            out.append(row)
        return out

    def _all_known_sessions_locked(self) -> list[str]:
        on_disk = readRegistry()
        return sorted(set(on_disk.keys()) | set(self.projects.keys()))

    async def _broadcast_to_clients(self, message: dict) -> None:
        if not self.websocket_clients:
            return
        payload = json.dumps(message, default=str)
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

    def __init__(self, taskmux_cli):  # type: ignore[no-untyped-def]
        self.taskmux_cli = taskmux_cli

    def watch_config(self) -> None:
        import time as _time

        print("Watching taskmux.toml for changes...")
        print("Press Ctrl+C to stop")

        loop = asyncio.new_event_loop()
        observer = Observer()
        cli = self.taskmux_cli
        watcher = ConfigWatcher(
            session=cli.config.name,
            config_path=cli.config_path,
            loop=loop,
            on_reload=lambda _s: cli.reload_config(),
        )
        observer.schedule(watcher, str(cli.config_path.parent), recursive=False)
        observer.start()

        try:
            while True:
                loop.call_soon(loop.stop)
                loop.run_forever()
                _time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
            print("\nStopped watching")

        observer.join()
        loop.close()


def list_running_projects() -> list[dict]:
    """Return the registry contents annotated with daemon status."""
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


def list_running_daemons() -> list[dict]:
    """Deprecated: returns the registered project list."""
    return list_running_projects()
