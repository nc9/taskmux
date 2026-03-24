# Taskmux

Tmux session manager for development environments. Define tasks in `taskmux.toml`, start them in dependency order, monitor health, auto-restart on failure, read persistent timestamped logs. Every command supports `--json` for agents and scripts.

## Features

- **Task orchestration** — start/stop/restart tasks with dependency ordering and signal escalation
- **Persistent logs** — timestamped, rotated, survives session kill (`~/.taskmux/logs/`)
- **Health checks** — custom commands with retries, used for dependency gating and auto-restart
- **Restart policies** — `no`, `on-failure` (default), `always` with exponential backoff
- **JSON output** — `--json` on every command for programmatic consumption
- **Event history** — lifecycle events recorded to `~/.taskmux/events.jsonl`
- **Lifecycle hooks** — before/after start/stop at global and per-task level
- **Port cleanup** — kills orphaned listeners before starting
- **Agent context** — `taskmux init` injects usage instructions into Claude/Codex/OpenCode context files
- **Daemon mode** — WebSocket API + config watching + health monitoring
- **Tmux native** — `tmux attach` to see live output, interact with tasks

## Install

Requires [tmux](https://github.com/tmux/tmux) and Python 3.11+.

```bash
uv tool install taskmux
```

## Commands

All commands support `--json` for machine-readable output.

```bash
# Lifecycle
taskmux start                    # start all auto_start tasks in dependency order
taskmux start <task> [task2...]  # start specific tasks
taskmux start -m                 # start + monitor health + auto-restart
taskmux stop                     # graceful stop all (C-c → SIGTERM → SIGKILL)
taskmux stop <task> [task2...]   # stop specific tasks
taskmux restart                  # restart all
taskmux restart <task>           # restart specific tasks
taskmux kill <task>              # hard-kill (SIGKILL + destroy window)

# Info
taskmux status                   # task overview (aliases: list, ls)
taskmux health                   # health check table
taskmux inspect <task>           # full task state as JSON
taskmux events                   # recent lifecycle events
taskmux events --task server     # filter by task
taskmux events --since 1h        # filter by time

# Logs — persistent, timestamped, stored at ~/.taskmux/logs/
taskmux logs                     # interleaved logs from all tasks
taskmux logs <task>              # logs for one task
taskmux logs -f [task]           # follow live
taskmux logs -n 200 <task>       # last N lines
taskmux logs -g "error"          # grep all tasks
taskmux logs -g "err" -C 5      # grep with context
taskmux logs --since 5m          # last 5 minutes
taskmux logs --since "2024-01-01T14:00"
taskmux logs-clean [task]        # delete log files

# Config
taskmux add <task> "<command>"   # add task to taskmux.toml
taskmux remove <task>            # remove task (kills if running)
taskmux init                     # create taskmux.toml + inject agent context
taskmux init --defaults          # non-interactive

# Monitoring
taskmux watch                    # watch config, reload on change
taskmux daemon --port 8765       # daemon: WebSocket API + health + config watch
```

### stop vs kill vs restart

| Command | Signal | Window | Auto-restart |
|---------|--------|--------|--------------|
| `stop` | C-c → SIGTERM → SIGKILL | Stays alive | Blocked |
| `kill` | SIGKILL | Destroyed | Blocked |
| `restart` | Full stop + restart | Reused | Re-enabled |

`stop` and `kill` mark tasks as manually stopped — no auto-restart even with `restart_policy = "always"`. `restart` or `start` clears this flag.

## Configuration

Config file: `taskmux.toml` in the current directory.

### Minimal

```toml
name = "myproject"

[tasks.server]
command = "npm run dev"

[tasks.build]
command = "npm run build:watch"

[tasks.db]
command = "docker compose up postgres"
auto_start = false
```

### Full-stack example

```toml
name = "fullstack-app"

[tasks.db]
command = "docker compose up postgres redis"
health_check = "pg_isready -h localhost -p 5432"
health_interval = 3

[tasks.migrate]
command = "python manage.py migrate && echo done && sleep infinity"
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

On `taskmux start`: db starts first → migrate + worker wait for db health → api waits for migrate → web waits for api → storybook skipped (manual).

### Fields

| Field | Default | Description |
|-------|---------|-------------|
| `name` | `"taskmux"` | tmux session name |
| `auto_start` | `true` | global toggle — `false` creates session but launches nothing |
| `hooks.*` | — | `before_start`, `after_start`, `before_stop`, `after_stop` |
| **Task fields** | | |
| `command` | required | shell command to run |
| `auto_start` | `true` | include in `taskmux start` |
| `cwd` | — | working directory |
| `port` | — | port to clean up before starting |
| `health_check` | — | shell command (exit 0 = healthy) |
| `health_interval` | `10` | seconds between checks |
| `health_timeout` | `5` | seconds before check times out |
| `health_retries` | `3` | consecutive failures before restart |
| `stop_grace_period` | `5` | seconds after C-c before SIGTERM |
| `restart_policy` | `"on-failure"` | `"no"`, `"on-failure"`, `"always"` |
| `max_restarts` | `5` | max restarts before giving up (resets after 60s healthy) |
| `restart_backoff` | `2.0` | exponential backoff base (capped 60s) |
| `log_file` | — | override log path (default: `~/.taskmux/logs/{session}/{task}.log`) |
| `log_max_size` | `"10MB"` | max size before rotation |
| `log_max_files` | `3` | rotated files to keep |
| `depends_on` | `[]` | tasks that must be healthy first |
| `hooks.*` | — | per-task lifecycle hooks |

## JSON Output

Every command supports `--json`. Key schemas:

```bash
taskmux status --json            # {"session": "x", "running": true, "tasks": [...]}
taskmux health --json            # {"healthy_count": 2, "total_count": 3, "tasks": [...]}
taskmux start server --json      # {"ok": true, "task": "server", "action": "started"}
taskmux logs server --json       # {"task": "server", "lines": ["2024-01-01T14:00:00 ..."]}
taskmux events --json            # {"events": [...], "count": 10}
```

Error: `{"ok": false, "error": "Task 'ghost' not found in config"}`

## Restart Policies

Enforced by `start --monitor` and `daemon`.

| Policy | Behavior |
|--------|----------|
| `"no"` | Never auto-restart |
| `"on-failure"` | **(default)** Restart on crash or after `health_retries` consecutive failures |
| `"always"` | Restart on any exit (including clean) |

`restart_policy` and `auto_start` are orthogonal — `auto_start` controls initial launch, `restart_policy` controls what happens after exit.

Backoff: `restart_backoff ^ attempt` seconds (capped 60s). Resets after 60s healthy. Stops after `max_restarts`.

## Health Checks

If `health_check` is set, taskmux runs it as a shell command (exit 0 = healthy). Falls back to checking if the pane has a running process. Must fail `health_retries` consecutive times before triggering restart.

Used by:
- `taskmux health` — status table
- `taskmux start` — dependency gating
- `start --monitor` / `daemon` — auto-restart trigger

## Persistent Logs

Task output is piped to `~/.taskmux/logs/{session}/{task}.log` with UTC timestamps:

```
2024-01-01T14:00:00.123 Server started on port 3000
2024-01-01T14:00:01.456 GET /health 200 2ms
```

Logs survive session kill. Rotated at `log_max_size` (default 10MB), keeping `log_max_files` (default 3). Filter with `--since`:

```bash
taskmux logs server --since 5m
taskmux logs --since 1h
```

## Event History

Lifecycle events at `~/.taskmux/events.jsonl`:

| Event | Trigger |
|-------|---------|
| `task_started` / `task_stopped` / `task_restarted` / `task_killed` | CLI commands |
| `session_started` / `session_stopped` | start/stop all |
| `health_check_failed` | health check fails (includes attempt count) |
| `auto_restart` | restart triggered (includes reason) |
| `max_restarts_reached` | hit limit |
| `config_reloaded` | config file changed |

Auto-trims to 10K lines at 15K.

## Hooks

Fire order: global `before_start` → task `before_start` → run → task `after_start` → global `after_start`. Same for stop. `before_*` failure aborts the action.

```toml
[hooks]
before_start = "echo starting"

[tasks.api.hooks]
before_start = "python manage.py migrate"
after_stop = "echo api stopped"
```

## Daemon & WebSocket API

```bash
taskmux daemon --port 8765
```

Health monitoring every 30s, auto-restart per policy, config file watching. WebSocket API:

```javascript
ws.send(JSON.stringify({ command: "status" }));
ws.send(JSON.stringify({ command: "restart", params: { task: "server" } }));
ws.send(JSON.stringify({ command: "logs", params: { task: "server", lines: 50 } }));
```

## Tmux

Taskmux creates standard tmux sessions:

```bash
tmux attach-session -t myproject   # attach to see live output
# Ctrl+b 1/2/3 switch windows, Ctrl+b d detach
```

## Links

- [PyPI](https://pypi.org/project/taskmux/)
- [GitHub](https://github.com/nc9/taskmux)

## License

MIT
