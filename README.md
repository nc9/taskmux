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

Create a `taskmux.toml` in your project root:

```toml
name = "myproject"

[tasks.server]
command = "npm run dev"

[tasks.build]
command = "npm run build:watch"

[tasks.test]
command = "npm run test:watch"

[tasks.db]
command = "docker compose up postgres"
auto_start = false
```

Tasks have `auto_start = true` by default. Set `auto_start = false` for tasks you want to start manually.

```bash
# Start all auto_start tasks
taskmux start

# Check status
taskmux list

# Restart a task
taskmux restart server

# Follow logs
taskmux logs -f test
```

## Commands

```bash
# Session
taskmux start                    # Start all auto_start tasks
taskmux stop                     # Stop session and all tasks
taskmux status                   # Show session status
taskmux list                     # List tasks with health indicators

# Tasks
taskmux restart <task>           # Restart a task
taskmux kill <task>              # Kill a task
taskmux add <task> "<command>"   # Add task to config
taskmux remove <task>            # Remove task from config

# Monitoring
taskmux health                   # Health check table
taskmux logs <task>              # Show recent logs
taskmux logs -f <task>           # Follow logs live
taskmux logs -n 100 <task>       # Last N lines

# Advanced
taskmux watch                    # Watch config for changes, reload on edit
taskmux daemon --port 8765       # Run with WebSocket API + auto-restart
```

## Configuration

### Format

Config file is `taskmux.toml` in the current directory:

```toml
name = "session-name"

[tasks.server]
command = "python manage.py runserver"

[tasks.worker]
command = "celery worker -A myapp"

[tasks.tailwind]
command = "npx tailwindcss -w"
auto_start = false
```

### Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | yes | — | tmux session name |
| `tasks.<name>.command` | yes | — | Shell command to run |
| `tasks.<name>.auto_start` | no | `true` | Start with `taskmux start` |

### Managing tasks via CLI

```bash
# Add a task (auto_start defaults to true)
taskmux add redis "redis-server"

# Remove a task (kills it if running)
taskmux remove redis
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
