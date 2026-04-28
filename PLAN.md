# Rip out tmux, daemon becomes supervisor (docker model)

## Context

Tmux is doing PTY allocation, process supervision, and log capture for taskmux today. User never attaches; tmux isn't installed by default on macOS; it's now friction. Goal: daemon owns all task processes (docker-like — daemon shutdown = all tasks shut down). Drop `libtmux` and `_log_pipe.py`. CLI becomes thin client over existing daemon IPC.

Locked decisions:
- **Daemon-only supervision.** CLI auto-starts daemon if absent. All commands are IPC calls.
- **Orphan-on-SIGKILL accepted** on macOS (no prctl). Graceful daemon shutdown kills all process groups; hard kill orphans (documented).
- **Posix-only now, Windows seam preserved.** Ship `PosixSupervisor` (mac+linux). Define a `Supervisor` protocol so a future `WindowsSupervisor` (ConPTY + Job Objects) drops in without architectural rework. Linux is a free win — same code path as mac, with optional `prctl(PR_SET_PDEATHSIG)` upgrade for orphan-proofing.

## Architecture

### New: `taskmux/supervisor.py`
Defines the `Supervisor` protocol (start/stop/restart/kill/inspect/list/show_logs/check_health/auto_restart, returning result dicts) and ships `PosixSupervisor` as the only concrete implementation today. Drop-in replacement for `TmuxManager` public surface — same method names, same result-dict returns, so `cli.py` callsites barely change in shape.

Platform selection: `make_supervisor()` factory returns `PosixSupervisor` on Darwin/Linux, raises `NotImplementedError` on Windows with a pointer to the seam. Linux gets an optional `prctl(PR_SET_PDEATHSIG, SIGTERM)` in the child setup hook (guarded by `platform.system() == "Linux"`) for stronger orphan prevention than mac.

Per-task state: `TaskProcess = { proc, master_fd, pgid, log_task, exit_task, started_at }`.

Spawn flow:
1. `master_fd, slave_fd = os.openpty()`
2. Launch via `asyncio.create_subprocess_*` API with `/bin/sh -c <command>`, slave_fd wired to stdin/stdout/stderr, `cwd=resolved_cwd`, env passed through, `start_new_session=True` (gives setsid → own pgrp + controlling tty for slave).
3. `os.close(slave_fd)` in parent.
4. Schedule log task: `loop.add_reader(master_fd, _drain)` → line-buffered timestamped write to `~/.taskmux/projects/{project}/logs/{task}.log` with rotation (port logic from `_log_pipe.py`).
5. Schedule exit task: `await proc.wait()` → emit `task_exited` event → fire `RestartTracker` policy → restart or stop.
6. Emit `task_started` event; notify proxy via existing `on_task_route_change`.

Stop flow (preserves today's escalation in `tmux_manager.stop_task`):
- `os.killpg(pgid, SIGINT)` → wait `stop_grace` → `SIGTERM` → wait → `SIGKILL`.
- Drain log_task, close master_fd. Mark manually-stopped in `RestartTracker`.

Reused unchanged:
- `RestartTracker` (lines 93–136 of `tmux_manager.py`) — lift verbatim into `supervisor.py`.
- HTTP/TCP/shell health probes (`check_health` lines 414–441) — unchanged. Pane-alive fallback → `proc.returncode is None`.
- Topological dependency sort in `start_all` — lift verbatim.
- Event emission via `taskmux/events.py` — unchanged.
- Log file paths, format, rotation knobs (`log_max_size`, `log_max_files`) — preserved.

### Modified: `taskmux/daemon.py`
- Daemon owns one `Supervisor` per loaded project (today the CLI does). Existing per-project loading stays; ownership shifts.
- Add IPC endpoints (extend existing aiohttp/WebSocket surface) for every CLI verb: `start`, `stop`, `restart`, `kill`, `status`, `inspect`, `health`, `events`, `clean`, `logs` (stream), `add`, `remove`. Reuse the existing JSON request/response shape.
- `signal.SIGTERM`/`SIGINT` handler → `await all_supervisors.stop_all(grace=...)` → exit.
- Drop the `libtmux.Server` socket-env handling around line 660.

### New: `taskmux/ipc_client.py`
- Thin client used by `cli.py`. Sends JSON request to daemon socket; returns dict.
- `ensureDaemonRunning()`: ping `/health` (or equivalent); if no response, background-spawn the daemon module (`sys.executable -m taskmux daemon`) with `start_new_session=True` and stdout/stderr to DEVNULL; poll up to ~2s for readiness.
- All CLI commands route through this. `--json` flag still works (daemon returns JSON; CLI prints it).

### Modified: `taskmux/cli.py`
- Drop `from .tmux_manager import TmuxManager` and the `cli.tmux = TmuxManager(...)` construction (line 100).
- Each command body becomes: `result = ipc_client.call("start", task=t, ...); print_result(result)`.
- `daemon` subcommand stays — it's the supervisor host.
- `logs --follow`: stream via WebSocket from daemon (daemon tails its own log file and forwards lines).

### Deleted
- `taskmux/tmux_manager.py` (1340 lines).
- `taskmux/_log_pipe.py` (77 lines).
- `libtmux` from `pyproject.toml`.
- `tests/test_tmux_manager.py` (26 tests) → replaced by `tests/test_supervisor.py`.

## Critical files
- `taskmux/supervisor.py` (new)
- `taskmux/ipc_client.py` (new)
- `taskmux/daemon.py` (extend IPC + own supervisors + signal handler)
- `taskmux/cli.py` (thin-client refactor)
- `pyproject.toml` (drop `libtmux`)
- `tests/test_supervisor.py` (new), `tests/test_cli.py` (re-mock to ipc_client), `tests/test_daemon_api.py` (extend)

## Build sequence
1. **Supervisor in isolation** — `supervisor.py` + `RestartTracker` lift + log writer + tests. No daemon/cli changes yet. `make test` green.
2. **Daemon hosts supervisor** — daemon constructs `Supervisor` per project; expose IPC endpoints; SIGTERM handler kills all groups. Daemon tests extended.
3. **IPC client + CLI rewrite** — `ipc_client.py` + auto-start; `cli.py` switched verb-by-verb. CLI tests re-mocked.
4. **Smoke test** end-to-end on a real project (see verification).
5. **Delete tmux** — `tmux_manager.py`, `_log_pipe.py`, drop dep, delete `test_tmux_manager.py`.
6. `make check` clean.

## Verification

End-to-end (manual) on a real `taskmux.toml` with 2+ tasks:
1. `taskmux start` (no daemon running) → daemon auto-starts → tasks running.
2. `ps -ef | grep taskmuxd` shows daemon; `pgrep -P <daemon_pid>` shows task pgrps.
3. `taskmux logs <task> --follow` streams live output; ANSI colors preserved (proves PTY).
4. Run a task that checks `isatty()` (e.g., `python -c "import sys; print(sys.stdout.isatty())"`) → `True`.
5. `taskmux stop <task>` → process group gone; restart-policy honored on crash.
6. `kill -TERM <daemon_pid>` → all task pgrps die within `stop_grace`. ✅
7. `kill -9 <daemon_pid>` → tasks orphan (documented behavior). Confirm `taskmux clean` (or new `reap`) recovers.
8. Restart a task whose command writes >log_max_size bytes → rotation works.
9. Health-failing task with `restart_policy = "on-failure"` → auto-restarts up to limit; events recorded.

Automated:
- `make check` (ruff + basedpyright + pytest).
- New `test_supervisor.py`: mock the asyncio subprocess API + `os.openpty`; cover start/stop/restart/kill/exit-detected-restart/manual-stop-overrides-policy/log rotation/health fallback.
- IPC integration test: spin daemon in-process, drive via `ipc_client`, assert state transitions.

## Out of scope (call out, don't do)
- Interactive attach (no `taskmux attach`). Drop entirely.
- `capture-pane` fallback in `logs` — gone; we always have a real log file now.
- SIGWINCH/window-size relay — not needed without attach.
- Windows backend — protocol seam in place; `WindowsSupervisor` (ConPTY via `pywinpty` + Job Objects with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` + `CTRL_BREAK_EVENT` for graceful stop) is a future ticket. Path handling will need `platformdirs` at that point.
