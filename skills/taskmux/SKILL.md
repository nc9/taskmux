---
name: taskmux
description: Manage long-running dev tasks (servers, watchers, build processes) via taskmux — a tmux-backed task runner driven by `taskmux.toml`. TRIGGER when cwd has `taskmux.toml`, when the user mentions taskmux, or when the user wants to start/stop/inspect/tail-logs of long-running processes in a project that has taskmux configured. SKIP for one-shot commands (tests, builds) and for projects without `taskmux.toml`.
---

# taskmux

Tmux-backed task runner. Tasks are declared in `taskmux.toml`; taskmux runs each in its own tmux window, mirrors output to a persistent timestamped log, and (when a daemon or `--monitor` is up) auto-restarts per `restart_policy`. Every command takes `--json`.

## When to invoke

- `taskmux.toml` exists in repo root → use taskmux for any long-running process (dev server, watcher, queue, db). Do NOT run those commands directly.
- User says "start the server", "tail logs", "what's running", "restart the watcher", "is X healthy" in a taskmux project.
- User asks to add/remove a task, or to debug a crash/restart loop.

Skip for: one-shot commands (`pytest`, `bun build`, `cargo test`), projects without `taskmux.toml`.

## Detection

```bash
test -f taskmux.toml && echo "taskmux project"
```

Also check `taskmux status --json` — if `running: false` you may need to start tasks first.

## CLI cheat sheet

Global flag: `--json` on every command for machine-readable output.

```bash
# Lifecycle
taskmux start                       # all auto_start tasks, dependency-ordered
taskmux start <task> [<task>...]    # specific tasks
taskmux start -m                    # + monitor (auto-restart, foreground)
taskmux start -d                    # + spawn detached daemon
taskmux stop [<task>...]            # graceful: C-c → SIGTERM → SIGKILL
taskmux restart [<task>...]         # full stop + start, clears manual-stop flag
taskmux kill <task>                 # SIGKILL + destroy window (blocks auto-restart)

# Inspect
taskmux status                      # overview (aliases: list, ls)
taskmux health                      # health-check table (-v for probe details)
taskmux inspect <task>              # full task state (always JSON-friendly)
taskmux events                      # lifecycle events (--task X, --since 1h)

# Logs (persistent, timestamped, at ~/.taskmux/projects/{session}/logs/)
taskmux logs                        # interleaved across all tasks
taskmux logs <task>                 # single task
taskmux logs -f [<task>]            # follow live
taskmux logs -n 200 <task>          # last N lines
taskmux logs -g "error"             # grep all tasks
taskmux logs -g "err" -C 5          # grep with context
taskmux logs --since 5m             # last 5 minutes
taskmux logs-clean [<task>]         # delete log files

# Config
taskmux add <task> "<cmd>"          # add to taskmux.toml
taskmux add api "next dev" --host api   # + proxy URL
taskmux remove <task>               # remove (kills if running)

# Daemon (auto-restart + WebSocket API)
taskmux daemon start | stop | status | restart | list
taskmux daemon register             # add cwd's project to global registry
```

## JSON patterns

```bash
# Quick status
taskmux status --json | jq '.tasks[] | {name, state, healthy}'

# Find unhealthy tasks
taskmux health --json | jq '.tasks[] | select(.healthy == false)'

# Why did a task fail?
taskmux inspect <task> --json
taskmux events --task <task> --json | jq '.events[-5:]'
taskmux logs <task> --since 10m -g "error|exception|fatal"

# Did anything restart recently?
taskmux events --since 1h --json | jq '.events[] | select(.event == "auto_restart" or .event == "max_restarts_reached")'
```

Result shape: `{"ok": true|false, ...}`. On error: `{"ok": false, "error": "..."}`.

## Common workflows

### "Server died, what happened?"
1. `taskmux status --json` → confirm task state.
2. `taskmux events --task <task> --since 1h --json` → look for `health_check_failed`, `auto_restart`, `max_restarts_reached`.
3. `taskmux logs <task> --since 30m -g "error|panic|exception"` → root cause.
4. `taskmux inspect <task> --json` → restart count, last failure reason.

### "Add a new dev task"
```bash
taskmux add worker "celery -A app worker -l info"
taskmux start worker
taskmux logs -f worker
```
For HTTP-style tasks behind the proxy, add `--host api` so it gets `https://api.{project}.localhost`.

### "Change restart behavior"
Edit `taskmux.toml` directly:
```toml
[tasks.worker]
restart_policy = "always"   # "no" | "on-failure" (default) | "always"
max_restarts = 10
restart_backoff = 3.0
```
Then `taskmux restart worker`. The daemon picks up config changes via the file watcher.

### "Run something long that's not in config"
Don't. Add it as a task first (`taskmux add`), then `taskmux start <name>`. This is the whole point — persistent logs, restarts, and dependency ordering only work for declared tasks.

## Anti-patterns — DO NOT

- ❌ `npm run dev &` / `cargo watch ... &` — backgrounded shell processes have no logs, no restart, no visibility. Use `taskmux start <task>`.
- ❌ `kill -9 <pid>` of a taskmux-managed pane — bypasses manual-stop tracking; auto-restart will re-spawn it. Use `taskmux stop <task>` or `taskmux kill <task>`.
- ❌ Reading raw tmux pane output (`tmux capture-pane`) for log analysis — use `taskmux logs <task> --grep` against the persistent log files.
- ❌ Editing `taskmux.toml` while expecting hot-reload without a daemon — the watcher only runs in `taskmux daemon` or `start --monitor`.
- ❌ Running `taskmux init` in a repo that already has `taskmux.toml` — it's a no-op and will print "Config already exists".

## Filesystem

```
~/.taskmux/
  projects/{session}/logs/{task}.log[.N]   # rotated, timestamped
  events.jsonl                             # cross-project lifecycle events
  registry.json                            # daemon-managed projects
  daemon.pid, daemon.log
  config.toml                              # global host config (optional)
```

Read these directly when CLI doesn't expose what you need (e.g. tailing a log file from a different process).

## Config reference (top-level)

```toml
name = "myproject"
auto_start = true        # global toggle
auto_daemon = false      # spawn daemon on `taskmux start`

[tasks.<name>]
command = "..."           # required
auto_start = true
cwd = "..."               # relative to taskmux.toml dir
host = "api"              # → https://api.{name}.localhost; $PORT injected
depends_on = []
health_url = "http://localhost:$PORT/health"
health_expected_status = 200
health_expected_body = "..."   # regex; catches dev-server "200 with error page"
health_check = "..."           # shell, exit 0 = healthy (used if no health_url)
health_interval = 10
health_retries = 3
restart_policy = "on-failure"  # "no" | "on-failure" | "always"
max_restarts = 5
restart_backoff = 2.0
stop_grace_period = 5
log_max_size = "10MB"
log_max_files = 3
```

## stop vs kill vs restart

| Command   | Signal                  | Window     | Auto-restart |
|-----------|-------------------------|------------|--------------|
| `stop`    | C-c → SIGTERM → SIGKILL | Stays      | Blocked      |
| `kill`    | SIGKILL                 | Destroyed  | Blocked      |
| `restart` | Full stop + start       | Reused     | Re-enabled   |

`stop`/`kill` set a manual-stop flag — restart policy is suppressed until `start` or `restart` clears it.
