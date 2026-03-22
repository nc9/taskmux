# Taskmux

A modern tmux session manager for LLM development tools with health monitoring, restart policies, and WebSocket API.

## Why Taskmux?

LLM coding tools like Claude Code and Cursor struggle with background tasks. Taskmux provides an LLM-friendly CLI for managing multiple background processes — restarting, checking status, reading logs — all from within your AI coding environment.

## Installation

### Prerequisites

- [tmux](https://github.com/tmux/tmux)
- [uv](https://docs.astral.sh/uv/) (Python 3.11+)

### Install

```bash
# Recommended (global install)
uv tool install taskmux

# From source
git clone https://github.com/nc9/taskmux
cd taskmux
uv tool install .
```

## Quick Start

```bash
# Initialize in your project (creates taskmux.toml, injects agent context)
taskmux init

# Add tasks
taskmux add server "npm run dev"
taskmux add build "npm run build:watch"
taskmux add db "docker compose up postgres"

# Start all auto_start tasks
taskmux start

# Check status
taskmux status
```

Or create a `taskmux.toml` manually:

```toml
name = "myproject"

[hooks]
before_start = "echo starting stack"
after_stop = "echo stack stopped"

[tasks.server]
command = "npm run dev"

[tasks.server.hooks]
before_start = "npm run build"

[tasks.build]
command = "npm run build:watch"

[tasks.test]
command = "npm run test:watch"

[tasks.db]
command = "docker compose up postgres"
auto_start = false
```

## Full Example

A full-stack app with a database, API server, and frontend — using health checks to ensure each service is ready before starting its dependents:

```toml
name = "fullstack-app"

[tasks.db]
command = "docker compose up postgres redis"
health_check = "pg_isready -h localhost -p 5432"
health_interval = 3

[tasks.migrate]
command = "python manage.py migrate && echo 'done' && sleep infinity"
cwd = "apps/api"
depends_on = ["db"]
health_check = "test -f .migrate-complete"

[tasks.api]
command = "python manage.py runserver 0.0.0.0:8000"
cwd = "apps/api"
port = 8000
depends_on = ["migrate"]
health_check = "curl -sf http://localhost:8000/health"
stop_grace_period = 10

[tasks.worker]
command = "celery -A myapp worker -l info"
cwd = "apps/api"
depends_on = ["db"]
restart_policy = "always"
max_restarts = 10
restart_backoff = 3.0

[tasks.web]
command = "bun dev"
cwd = "apps/web"
port = 3000
depends_on = ["api"]
health_check = "curl -sf http://localhost:3000"

[tasks.storybook]
command = "bun storybook"
cwd = "apps/web"
auto_start = false
```

What happens on `taskmux start`:

1. **db** starts first (no dependencies)
2. **migrate** and **worker** wait for db's health check (`pg_isready`) to pass
3. **api** waits for migrate's health check
4. **web** waits for api's health check (`curl localhost:8000/health`)
5. **storybook** is skipped (`auto_start = false`) — start it manually with `taskmux start storybook`

```bash
taskmux start                    # Starts everything in dependency order
taskmux logs                     # Interleaved logs from all tasks
taskmux logs -g "ERROR"          # Grep all tasks for errors
taskmux logs api                 # Logs from just the API
taskmux logs -f api              # Follow API logs live
taskmux health                   # Health check table
taskmux inspect api              # JSON state for a single task
taskmux restart worker           # Restart just the worker
taskmux start storybook          # Start a manual task
```

## Commands

```bash
# Session lifecycle
taskmux start                    # Start all auto_start tasks in dependency order
taskmux start <task> [task2...]  # Start specific tasks
taskmux start -m                 # Start + stay in foreground monitoring health/restarting
taskmux stop                     # Stop all (C-c → SIGTERM → SIGKILL), prevents auto-restart
taskmux stop <task> [task2...]   # Stop specific tasks
taskmux restart                  # Restart all tasks
taskmux restart <task> [task2...] # Restart specific tasks, re-enables auto-restart

# Task management
taskmux kill <task>              # Hard-kill (SIGKILL + destroy window), prevents auto-restart
taskmux add <task> "<command>"   # Add task to taskmux.toml
taskmux remove <task>            # Remove task (kills if running)
taskmux inspect <task>           # JSON state: pid, health, restart_policy, pane info

# Status & health
taskmux status                   # Session + task overview (aliases: list, ls)
taskmux health                   # Health check table for all tasks

# Logs
taskmux logs                     # Interleaved logs from all tasks
taskmux logs <task>              # Recent logs for a task
taskmux logs -f [task]           # Follow logs live (colored prefixes)
taskmux logs -n 200 <task>       # Last N lines
taskmux logs -g "error"          # Grep all tasks
taskmux logs <task> -g "err" -C 5  # Grep one task with context

# Setup & monitoring
taskmux init                     # Interactive project setup + agent context injection
taskmux init --defaults          # Non-interactive setup
taskmux watch                    # Watch taskmux.toml, reload on change
taskmux daemon --port 8765       # Daemon mode: WebSocket API + health monitoring
```

### stop vs kill vs restart

| Command | Signal | Window | Auto-restart |
|---------|--------|--------|--------------|
| `stop` | C-c → SIGTERM → SIGKILL (graceful) | Stays alive | Blocked (manually stopped) |
| `kill` | SIGKILL (immediate) | Destroyed | Blocked (manually stopped) |
| `restart` | Full stop + restart | Reused | Re-enabled |

Both `stop` and `kill` mark the task as **manually stopped**, preventing auto-restart even with `restart_policy = "always"`. Use `restart` or `start` to clear this flag and re-enable auto-restart.

## Configuration

### Format

Config file is `taskmux.toml` in the current directory:

```toml
name = "session-name"
auto_start = true       # global toggle, default true

[hooks]
before_start = "echo starting"
after_stop = "echo done"

[tasks.server]
command = "python manage.py runserver"
cwd = "apps/api"
port = 8000
health_check = "curl -sf http://localhost:8000/health"
stop_grace_period = 10
depends_on = ["db"]

[tasks.server.hooks]
before_start = "python manage.py migrate"

[tasks.db]
command = "docker compose up postgres"
health_check = "pg_isready -h localhost"

[tasks.worker]
command = "celery worker -A myapp"
depends_on = ["db"]
restart_policy = "always"
max_restarts = 10

[tasks.tailwind]
command = "npx tailwindcss -w"
auto_start = false
restart_policy = "no"
```

### Fields

| Field | Default | Description |
|-------|---------|-------------|
| `name` | `"taskmux"` | tmux session name |
| `auto_start` | `true` | Global toggle — if false, `start` creates session but launches nothing |
| `hooks.before_start` | — | Run before starting tasks |
| `hooks.after_start` | — | Run after starting tasks |
| `hooks.before_stop` | — | Run before stopping tasks |
| `hooks.after_stop` | — | Run after stopping tasks |
| `tasks.<name>.command` | — | Shell command to run |
| `tasks.<name>.auto_start` | `true` | Start with `taskmux start` |
| `tasks.<name>.cwd` | — | Working directory for the task |
| `tasks.<name>.port` | — | Port to clean up before starting (kills orphaned listeners) |
| `tasks.<name>.health_check` | — | Shell command to check health (exit 0 = healthy) |
| `tasks.<name>.health_interval` | `10` | Seconds between health checks |
| `tasks.<name>.health_timeout` | `5` | Seconds before health check times out |
| `tasks.<name>.health_retries` | `3` | Consecutive health failures before triggering a restart |
| `tasks.<name>.stop_grace_period` | `5` | Seconds to wait after C-c before escalating to SIGTERM |
| `tasks.<name>.restart_policy` | `"on-failure"` | When to auto-restart: `"no"`, `"on-failure"`, or `"always"` (see below) |
| `tasks.<name>.max_restarts` | `5` | Max auto-restarts before giving up (resets after 60s healthy) |
| `tasks.<name>.restart_backoff` | `2.0` | Exponential backoff base for restart delay (1s, 2s, 4s… capped at 60s) |
| `tasks.<name>.depends_on` | `[]` | Task names that must be healthy before this task starts |
| `tasks.<name>.hooks.*` | — | Per-task lifecycle hooks (same fields as global) |

### Dependency Ordering

Tasks with `depends_on` are started in topological order. Before starting a task, taskmux waits for each dependency's health check to pass (up to `health_retries * health_interval` seconds). If a dependency never becomes healthy, the dependent task is skipped with a warning.

Circular dependencies and references to nonexistent tasks are rejected at config load time.

When starting a single task with `taskmux start <task>`, dependencies are not auto-started — you get a warning if they aren't running.

### Restart Policies

Each task has a `restart_policy` that controls automatic restart behavior. Restart policies are enforced by `taskmux start --monitor` and `taskmux daemon`.

| Policy | Behavior |
|--------|----------|
| `"no"` | Never auto-restart. Task stays stopped after crash or health failure. |
| `"on-failure"` | **(default)** Restart on crash (process exits) or after `health_retries` consecutive health check failures. |
| `"always"` | Restart whenever the task stops, including clean exits. |

**Manual stops override all policies.** Running `taskmux stop` or `taskmux kill` marks the task as manually stopped — it will not auto-restart even with `restart_policy = "always"`. Use `taskmux restart` or `taskmux start` to clear this flag.

**`restart_policy` vs `auto_start`** — these are orthogonal. `auto_start` controls whether a task launches on `taskmux start`. `restart_policy` controls what happens after a running task exits or fails. A task with `auto_start = false` and `restart_policy = "always"` won't start automatically, but once started manually, it will auto-restart on exit.

| `restart_policy` | `auto_start` | Behavior |
|---|---|---|
| `"no"` | `true` | Starts with session, never auto-restarts |
| `"no"` | `false` | Manual start only, never auto-restarts |
| `"on-failure"` | `true` | Starts with session, restarts on crash/health failure |
| `"on-failure"` | `false` | Manual start, restarts on crash/health failure once running |
| `"always"` | `true` | Starts with session, restarts on any exit |
| `"always"` | `false` | Manual start, restarts on any exit once running |

**Backoff & limits:** When a task keeps failing, restart delays increase exponentially: `restart_backoff ^ attempt` seconds (capped at 60s). After `max_restarts` consecutive restarts, the task is left stopped. The restart counter resets after 60 seconds of healthy uptime.

### Health Checks

If `health_check` is set, taskmux runs it as a shell command. Exit code 0 means healthy. If not set, taskmux falls back to checking if the tmux pane has a running process (not just a shell prompt).

A task must fail `health_retries` consecutive health checks (default 3) before being considered unhealthy and triggering a restart. If the task becomes healthy again, the failure counter resets.

Health checks are used by:
- `taskmux health` — shows a table of all task health
- `taskmux start` — waits for dependencies to be healthy before starting dependents
- `taskmux start --monitor` — continuously monitors and auto-restarts per restart_policy
- `taskmux daemon` — same as --monitor, plus WebSocket API and config watching

### Hook Cascade

Hooks fire in this order:
1. **Start**: global `before_start` → task `before_start` → _run command_ → task `after_start` → global `after_start`
2. **Stop**: global `before_stop` → task `before_stop` → _send C-c_ → task `after_stop` → global `after_stop`

If a `before_*` hook fails (non-zero exit), the action is aborted.

### Process Lifecycle

Taskmux ensures processes are fully stopped before restarting and that orphaned port listeners don't block new starts.

**Stop escalation** (`stop`, `restart`):

1. **C-c** (SIGINT) — waits `stop_grace_period` seconds (default 5)
2. **SIGTERM** to process group — waits 3 seconds
3. **SIGKILL** to process group — force kill

**Port cleanup** (`start`, `restart`): If `port` is configured, taskmux kills any process listening on that port before starting. This handles orphaned processes from crashed sessions.

**Auto-restart** (`start --monitor`, `daemon`): Tasks with `restart_policy = "on-failure"` or `"always"` are automatically restarted. Health checks must fail `health_retries` times before triggering a restart. Restart delays increase exponentially (`restart_backoff` base, capped at 60s). After `max_restarts` failures, the task is left stopped. The counter resets after 60 seconds of healthy uptime.

### Init & Agent Context

`taskmux init` bootstraps your project:
1. Creates `taskmux.toml` with session name (defaults to directory name)
2. Detects installed AI coding agents (Claude, Codex, OpenCode)
3. Injects taskmux usage instructions into agent context files:
   - Claude: `.claude/rules/taskmux.md`
   - Codex/OpenCode: `AGENTS.md`

Use `--defaults` to skip prompts (CI/automation).

### Inspect

`taskmux inspect <task>` returns JSON with task state:

```json
{
  "name": "api",
  "command": "python manage.py runserver 0.0.0.0:8000",
  "auto_start": true,
  "restart_policy": "on-failure",
  "cwd": "apps/api",
  "health_check": "curl -sf http://localhost:8000/health",
  "depends_on": ["db"],
  "running": true,
  "healthy": true,
  "pid": "12345",
  "pane_current_command": "python",
  "pane_current_path": "/home/user/project/apps/api",
  "window_id": "@1",
  "pane_id": "%1"
}
```

## Monitoring & Auto-restart

### start --monitor (lightweight)

Start tasks and stay in the foreground monitoring health:

```bash
taskmux start --monitor     # or: taskmux start -m
```

Checks health every 30 seconds and auto-restarts tasks according to their `restart_policy`. No WebSocket API — just monitoring and restart. Press Ctrl+C to stop monitoring (tasks keep running).

### Daemon Mode (full)

Run as a background daemon with WebSocket API, config watching, and auto-restart:

```bash
taskmux daemon              # Default port 8765
taskmux daemon --port 9000  # Custom port
```

The daemon monitors task health every 30 seconds. Tasks are restarted per their `restart_policy` with exponential backoff (controlled by `restart_backoff` and `max_restarts`). Tasks that stay healthy for 60+ seconds have their restart counter reset. Config file changes are detected and applied automatically.

WebSocket API:

```javascript
const ws = new WebSocket('ws://localhost:8765');

ws.send(JSON.stringify({ command: "status" }));
ws.send(JSON.stringify({ command: "restart", params: { task: "server" } }));
ws.send(JSON.stringify({ command: "logs", params: { task: "server", lines: 50 } }));
```

## Tmux Integration

Taskmux creates standard tmux sessions — all tmux commands work:

```bash
tmux attach-session -t myproject   # Attach to session
tmux list-sessions                 # List all sessions
# Ctrl+b 1/2/3 to switch windows, Ctrl+b d to detach
```

## License

MIT
