# Taskmux

A modern tmux development environment manager for LLM development tools with real-time health monitoring, auto-restart capabilities, and WebSocket API.

## Why Taskmux?

LLM coding tools like Claude Code and Cursor can struggle with background tasks required in development. Taskmux provides an LLM tool friendly interface to managing multiple background tasks, restarting them, checking on status, reading logs and more from within LLM coding tools.

The interface is a CLI and MCP server that is built to be called by both humans and LLMs.

Taskmux provides:

- **Dynamic task management**: Define tasks in JSON, manage via modern CLI
- **Health monitoring**: Real-time task health checks with visual indicators
- **Auto-restart**: Automatically restart failed tasks to keep development flowing
- **WebSocket API**: Real-time status updates and remote task management
- **Rich CLI**: Beautiful terminal output with Typer and Rich integration
- **File watching**: Automatically detects config changes and reloads tasks
- **Zero setup**: Single command installation with uv tool management

## Installation

### Prerequisites

- [tmux](https://github.com/tmux/tmux) - Terminal multiplexer
- [uv](https://docs.astral.sh/uv/) - Modern Python package manager

### Install uv (if you don't have it)

**Quick install** (macOS/Linux):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows**:
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Alternative methods**:
```bash
# Via Homebrew (macOS)
brew install uv

# Via pipx
pipx install uv

# Via WinGet (Windows)
winget install --id=astral-sh.uv -e
```

### Install Taskmux

**Recommended** (installs globally):
```bash
uv tool install taskmux
```

**From source**:
```bash
git clone https://github.com/your-repo/taskmux
cd taskmux
uv tool install .
```

After installation, `taskmux` command will be available globally.

## Quick Start

1. **Create config file** in your project root:

```json
{
  "name": "myproject",
  "tasks": {
    "server": "npm run dev",
    "build": "npm run build:watch",
    "test": "npm run test:watch",
    "db": "docker-compose up postgres"
  }
}
```

2. **Start all tasks**:
```bash
taskmux start
```

3. **Monitor and manage**:
```bash
taskmux list          # See what's running with health status
taskmux health        # Detailed health check table
taskmux restart server # Restart specific task
taskmux logs -f test   # Follow logs
```

## Commands Reference

### Core Commands

```bash
# Session Management
taskmux start                    # Start all tasks in tmux session
taskmux status                   # Show session and task status
taskmux list                     # List all tasks with health indicators
taskmux stop                     # Stop session and all tasks

# Task Management
taskmux restart <task>           # Restart specific task
taskmux kill <task>              # Kill specific task
taskmux add <task> "<command>"   # Add new task to config
taskmux remove <task>            # Remove task from config

# Monitoring
taskmux health                   # Health check with status table
taskmux logs <task>              # Show recent logs
taskmux logs -f <task>           # Follow logs (live)
taskmux logs -n 100 <task>       # Show last N lines

# Advanced
taskmux watch                    # Watch config for changes
taskmux daemon --port 8765       # Run with WebSocket API
```

### Command Examples

```bash
# Start development environment
taskmux start

# Check what's running with health status
taskmux list
# Output:
# Session: myproject
# ──────────────────────────────────────────────────
# 💚 Healthy  server          npm run dev
# 💚 Healthy  build           npm run build:watch
# 🔴 Unhealthy test           npm run test:watch
# 💚 Healthy  db              docker-compose up postgres

# Detailed health check
taskmux health
# ┏━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┓
# ┃ Status ┃ Task    ┃ Health    ┃
# ┡━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
# │ 💚     │ server  │ Healthy   │
# │ 💚     │ build   │ Healthy   │
# │ 🔴     │ test    │ Unhealthy │
# │ 💚     │ db      │ Healthy   │
# └────────┴─────────┴───────────┘

# Restart a misbehaving service
taskmux restart server

# Add a new background task
taskmux add worker "python background_worker.py"

# Follow logs for debugging
taskmux logs -f test

# Watch config file for changes
taskmux watch
```

## Configuration

### Config File Format

Create `taskmux.json` in your project root:

```json
{
  "name": "session-name",
  "tasks": {
    "task-name": "command to run",
    "another-task": "another command"
  }
}
```

### Config Examples

**Web Development**:
```json
{
  "name": "webapp",
  "tasks": {
    "frontend": "npm run dev",
    "backend": "python manage.py runserver",
    "database": "docker-compose up -d postgres",
    "redis": "redis-server",
    "worker": "celery worker -A myapp",
    "tailwind": "npx tailwindcss -w"
  }
}
```

## Advanced Features

### Daemon Mode with WebSocket API

Run Taskmux as a background daemon with real-time API:

```bash
# Start daemon on port 8765 (default)
taskmux daemon

# Custom port
taskmux daemon --port 9000
```

**WebSocket API Usage**:
```javascript
// Connect to WebSocket API
const ws = new WebSocket('ws://localhost:8765');

// Get status
ws.send(JSON.stringify({
  command: "status"
}));

// Restart task
ws.send(JSON.stringify({
  command: "restart",
  params: { task: "server" }
}));

// Get logs
ws.send(JSON.stringify({
  command: "logs",
  params: { task: "server", lines: 50 }
}));
```

### Health Monitoring & Auto-restart

Taskmux continuously monitors task health and can auto-restart failed processes:

- **Health indicators**: 💚 Healthy, 🔴 Unhealthy, ○ Stopped
- **Process monitoring**: Detects when tasks exit or become unresponsive
- **Auto-restart**: Daemon mode automatically restarts failed tasks
- **Health checks**: Run `taskmux health` for detailed status

## Workflow Integration

### Daily Development

```bash
# Morning: Start everything
taskmux start

# During development: Monitor health
taskmux health

# Restart services as needed
taskmux restart api
taskmux logs -f frontend

# Add new services on the fly
taskmux add monitoring "python monitor.py"

# Run with file watching for config changes
taskmux watch

# Evening: Stop everything
taskmux stop
```

### Tmux Integration

Taskmux creates standard tmux sessions. You can use all tmux commands:

```bash
# Attach to session
tmux attach-session -t myproject

# Switch between task windows
# Ctrl+b 1, Ctrl+b 2, etc.

# Create additional windows
tmux new-window -t myproject -n shell

# Detach and reattach later
# Ctrl+b d
tmux attach-session -t myproject
```

### Multiple Projects

Each project gets its own tmux session based on the `name` field:

```bash
# Project A (session: "webapp")
cd ~/projects/webapp
taskmux start

# Project B (session: "api")
cd ~/projects/api
taskmux start

# Both run simultaneously with separate sessions
tmux list-sessions
# webapp: 4 windows
# api: 2 windows
```

## Troubleshooting

### Common Issues

**Config not found**:
```bash
Error: Config file taskmux.json not found
```
- Ensure `taskmux.json` exists in current directory
- Check JSON syntax with `jq . taskmux.json`

**Session already exists**:
```bash
Session 'myproject' already exists
```
- Kill existing session: `taskmux stop`
- Or attach to it: `tmux attach-session -t myproject`

**Task not restarting**:
- Check if task name exists: `taskmux list`
- Verify session is running: `taskmux status`
- Check task health: `taskmux health`

**libtmux connection issues**:
- Ensure tmux is installed and in PATH
- Try restarting tmux server: `tmux kill-server`

### Debug Mode

View detailed tmux session information:
```bash
# Check if session exists
tmux has-session -t myproject

# List windows in session
tmux list-windows -t myproject

# View logs manually
tmux capture-pane -t myproject:taskname -p

# Check daemon logs
tail -f ~/.taskmux/daemon.log
```


## License

MIT License - feel free to modify and distribute.