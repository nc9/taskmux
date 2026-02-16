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
taskmux logs <task>              # Show recent logs
taskmux logs -f <task>           # Follow logs live
taskmux logs -n 100 <task>       # Last N lines
taskmux logs <task> -g "error"   # Search logs with grep
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

[tasks.server.hooks]
before_start = "python manage.py migrate"

[tasks.worker]
command = "celery worker -A myapp"

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
| `tasks.<name>.hooks.*` | — | Per-task lifecycle hooks (same fields as global) |

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
  "name": "server",
  "command": "npm run dev",
  "auto_start": true,
  "running": true,
  "healthy": true,
  "pid": "12345",
  "pane_current_command": "node",
  "pane_current_path": "/home/user/project",
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
