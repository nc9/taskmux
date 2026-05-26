# Changelog

All notable user-facing changes to taskmux. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). Dates are release tag dates
(`YYYY-MM-DD`).

## [0.9.4] — 2026-05-26

### Added
- **HTTP → HTTPS redirect on :80.** The daemon now binds a plain-HTTP
  listener alongside the HTTPS proxy and 301-redirects every request to
  `https://{host}{path}`. Typing `myproj.localhost` (no protocol) in the
  browser no longer dead-ends in `ERR_CONNECTION_REFUSED`. Configurable via
  `proxy_http_redirect_port` in `~/.taskmux/config.toml` (default `80`,
  set `0` to disable). Dual-stack (v4 + v6), pre-bound before privilege
  drop, non-fatal if the port is taken — HTTPS keeps working.

### Fixed
- **`taskmux daemon stop` / `restart` now reliably terminates the daemon.**
  Escalates `SIGTERM` → wait → `SIGKILL`, reaps zombies with non-blocking
  `waitpid`, and cleans the stale `daemon.pid` once the process is gone.
  Fixes a class of hangs where stop/restart appeared to succeed but
  `kill -0` kept seeing the (zombie) child.
- **Preflight warning covers the redirect port.** Running the daemon
  unprivileged with `proxy_http_redirect_port < 1024` now warns at start
  time instead of failing silently into the daemon log.

## [0.9.3] — 2026-05-13

### Added
- **`taskmux daemon` warns when `proxy_https_port` already has a listener**
  (with the holding process via `lsof`), so you don't have to grep the
  daemon log to find out why TLS won't bind.

### Fixed
- **`daemon.log` is now rotated** (size-capped), and noisy `websockets` /
  `uvicorn` loggers are pinned to `WARNING` — every WS poll no longer
  produces 2-3 INFO lines.
- **`config_missing` warning de-duped per session.** Previously each
  registry scan re-warned for the same missing config; now it warns once
  and re-warns only if the path changes.

## [0.9.2] — 2026-05-10

### Added
- **`taskmux env`** — print per-project env exports (e.g. host suffix,
  assigned ports) for shell use.
- **`taskmux start --if-stopped`** — no-op when the task is already
  running. Designed for worktree init hooks that shouldn't restart a
  healthy task.

## [0.9.1] — 2026-05-05

### Added
- **`taskmux inject`** — refresh the marked taskmux block in
  `CLAUDE.md` / `AGENTS.md` without re-running `add` / `remove`.
- **`taskmux mcp install` checkbox UI.** Questionary checkbox with
  project-scoped targets (`claude-project`, `cursor-project`,
  `codex-project`, `opencode-project`) pre-checked above a separator;
  user-global targets sit below as opt-in.
- **`cursor-project` and `opencode-project`** MCP install targets.

### Changed
- **Agent inject block is pointer-only.** Dropped the per-task table —
  agents pick up tasks from the live `taskmux status` call, not from the
  static markdown.

## [0.9.0] — 2026-05-04

### Added
- **Daemon-hosted MCP server** at `http://localhost:{api_port}/mcp/`.
  Streamable HTTP transport. Tools (`taskmux_status`, `taskmux_logs`,
  `taskmux_start/stop/restart/kill`, `taskmux_health`, `taskmux_events`,
  `taskmux_list_projects`), resources (`taskmux://status`,
  `taskmux://projects`, `taskmux://events/recent`,
  `taskmux://logs/{session}/{task}`), and push notifications on every
  lifecycle event (severity-mapped). Connections are pinned per project
  via `?session=<name>`; cross-project tool calls return
  `pin_violation`. Install via `taskmux mcp install`.

### Fixed
- **Worktree-aware event session keys** + edge-trigger on
  `max_restarts_reached` so a stuck supervisor doesn't spam the event
  bus.
