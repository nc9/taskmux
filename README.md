# Taskmux

A modern tmux session manager for LLM development tools with health monitoring, auto-restart, and WebSocket API.

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
depends_on = ["migrate"]
health_check = "curl -sf http://localhost:8000/health"

[tasks.worker]
command = "celery -A myapp worker -l info"
cwd = "apps/api"
depends_on = ["db"]

[tasks.web]
command = "bun dev"
cwd = "apps/web"
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
# Session
taskmux start                    # Start all auto_start tasks
taskmux start <task>             # Start a single task
taskmux stop                     # Stop all tasks (graceful C-c)
taskmux stop <task>              # Stop a single task (graceful C-c)
taskmux restart                  # Restart all tasks
taskmux restart <task>           # Restart a single task
taskmux status                   # Show session status
taskmux list                     # List tasks with health indicators

# Tasks
taskmux kill <task>              # Hard-kill a task (destroys window)
taskmux add <task> "<command>"   # Add task to config
taskmux remove <task>            # Remove task from config
taskmux inspect <task>           # JSON task state (pid, command, health)

# Logs
taskmux logs                     # Interleaved logs from all tasks
taskmux logs <task>              # Show recent logs for a task
taskmux logs -f                  # Attach to session (switch windows with tmux keybinds)
taskmux logs -f <task>           # Follow a task's logs live
taskmux logs -n 200 <task>       # Last N lines
taskmux logs -g "error"          # Search all tasks
taskmux logs <task> -g "error"   # Search one task
taskmux logs <task> -g "error" -C 5  # Grep with context lines

# Init
taskmux init                     # Interactive project setup
taskmux init --defaults          # Non-interactive, use defaults

# Monitoring
taskmux health                   # Health check table
taskmux watch                    # Watch config for changes, reload on edit
taskmux daemon --port 8765       # Run with WebSocket API + auto-restart
```

### stop vs kill

- **`stop`** sends C-c (graceful). Window stays alive so you can see exit output.
- **`kill`** destroys the window immediately.

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
health_check = "curl -sf http://localhost:8000/health"
depends_on = ["db"]

[tasks.server.hooks]
before_start = "python manage.py migrate"

[tasks.db]
command = "docker compose up postgres"
health_check = "pg_isready -h localhost"

[tasks.worker]
command = "celery worker -A myapp"
depends_on = ["db"]

[tasks.tailwind]
command = "npx tailwindcss -w"
auto_start = false
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
| `tasks.<name>.health_check` | — | Shell command to check health (exit 0 = healthy) |
| `tasks.<name>.health_interval` | `10` | Seconds between health checks |
| `tasks.<name>.health_timeout` | `5` | Seconds before health check times out |
| `tasks.<name>.health_retries` | `3` | Consecutive failures before "unhealthy" |
| `tasks.<name>.depends_on` | `[]` | Task names that must be healthy before this task starts |
| `tasks.<name>.hooks.*` | — | Per-task lifecycle hooks (same fields as global) |

### Dependency Ordering

Tasks with `depends_on` are started in topological order. Before starting a task, taskmux waits for each dependency's health check to pass (up to `health_retries * health_interval` seconds). If a dependency never becomes healthy, the dependent task is skipped with a warning.

Circular dependencies and references to nonexistent tasks are rejected at config load time.

When starting a single task with `taskmux start <task>`, dependencies are not auto-started — you get a warning if they aren't running.

### Health Checks

If `health_check` is set, taskmux runs it as a shell command. Exit code 0 means healthy. If not set, taskmux falls back to checking if the tmux pane has a running process (not just a shell prompt).

Health checks are used by:
- `taskmux health` — shows a table of all task health
- `taskmux start` — waits for dependencies to be healthy before starting dependents
- `taskmux daemon` — continuously monitors and auto-restarts unhealthy tasks

### Hook Cascade

Hooks fire in this order:
1. **Start**: global `before_start` → task `before_start` → _run command_ → task `after_start` → global `after_start`
2. **Stop**: global `before_stop` → task `before_stop` → _send C-c_ → task `after_stop` → global `after_stop`

If a `before_*` hook fails (non-zero exit), the action is aborted.

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

## Daemon Mode

Run as a background daemon with WebSocket API and auto-restart:

```bash
taskmux daemon              # Default port 8765
taskmux daemon --port 9000  # Custom port
```

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
