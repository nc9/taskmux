# Taskmux

Tmux session manager for development environments. Define tasks in `taskmux.toml`, start them in dependency order, monitor health, auto-restart on failure, read persistent timestamped logs. Every command supports `--json` for agents and scripts.

Designed to pair well with coding agents like Claude Code, Codex, and OpenCode — `taskmux init` injects usage instructions into their context files, and the JSON output + WebSocket API give agents a clean, machine-readable surface for managing dev environments.

## Features

- **Task orchestration** — start/stop/restart tasks with dependency ordering and signal escalation
- **Persistent logs** — timestamped, rotated, survives session kill (`~/.taskmux/projects/{session}/logs/`)
- **Health checks** — custom commands with retries, used for dependency gating and auto-restart
- **Restart policies** — `no`, `on-failure` (default), `always` with exponential backoff
- **JSON output** — `--json` on every command for programmatic consumption
- **Event history** — lifecycle events recorded to `~/.taskmux/events.jsonl`
- **Lifecycle hooks** — before/after start/stop at global and per-task level
- **HTTPS proxy** — `host = "api"` exposes a task at `https://api.{project}.localhost` with a trusted local cert (mkcert); taskmux assigns a free `$PORT` per task so config never pins ports. Apex (`@`) and wildcard (`*`) host routes supported alongside specific subdomains
- **Dynamic DNS** — optional in-process DNS server resolves any `*.localhost` to `127.0.0.1` (no `/etc/hosts` churn, no daemon restart when adding hosts)
- **Worktree-aware** — linked git worktrees auto-namespace their `project_id` (`myproject-feat-foo`) so logs, registry entries, and proxy URLs don't collide with the primary checkout
- **Port cleanup** — kills orphaned listeners before starting
- **Agent context** — `taskmux init` injects a thin pointer + project task table into Claude/Codex/OpenCode context files; install the [taskmux skill](#claude-code-skill) for richer Claude Code guidance loaded on demand
- **Daemon mode** — WebSocket API + config watching + health monitoring
- **Tmux native** — `tmux attach` to see live output, interact with tasks

## Install

Requires [tmux](https://github.com/tmux/tmux) and Python 3.11+.

```bash
uv tool install taskmux
```

### Claude Code skill

Generic CLI guidance (when to invoke, JSON patterns, anti-patterns) lives in a Claude Code skill at [`skills/taskmux/`](skills/taskmux/SKILL.md). Project task tables still come from `taskmux init` injection. Install via [vercel-labs/skills](https://github.com/vercel-labs/skills):

```bash
npx skills add nc9/taskmux --skill taskmux        # project: .claude/skills/
npx skills add nc9/taskmux --skill taskmux -g     # global:  ~/.claude/skills/
```

Codex/OpenCode users don't need the skill — `taskmux init` writes a self-contained `AGENTS.md` block.

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

# Logs — persistent, timestamped, stored at ~/.taskmux/projects/{session}/logs/
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
taskmux add api "next dev" --host api  # expose at https://api.{project}.localhost
taskmux remove <task>            # remove task (kills if running)
taskmux init                     # create taskmux.toml + inject agent context
taskmux init --defaults          # non-interactive

# URLs / proxy
taskmux url <task>               # print proxy URL for a task
taskmux ca install               # install local CA into system trust store (one-time)
taskmux ca mint                  # mint cert for the current project

# Monitoring
taskmux watch                    # watch config, reload on change
taskmux daemon --port 8765       # daemon: WebSocket API + health + config watch
```

## URL routing (HTTPS proxy)

Taskmux can front your dev tasks with a stable, trusted HTTPS URL — no port juggling:

```
https://api.myproject.localhost
https://web.myproject.localhost
```

Setup (one time):

```bash
brew install mkcert nss      # macOS; see mkcert install guide for other OSes
taskmux ca install            # trusts the local CA in your system store
sudo taskmux daemon           # binds :443 as root, then drops to your user.
                              # Everything after the bind (tmux, certs, state)
                              # runs as you, not root.
```

In your `taskmux.toml`, replace `port = 3000` style fields with `host = "web"` and read `$PORT` from the env in your command:

```toml
name = "myproject"

[tasks.api]
command = "next dev -p $PORT"
host = "api"

[tasks.web]
command = "bun dev --port $PORT"
host = "web"
```

The daemon picks a free port for each task, injects it as `$PORT`, and routes `https://{host}.{name}.localhost` to it. Browsers resolve `*.localhost` to `127.0.0.1` automatically. The cert is wildcarded over the project, so adding/removing tasks doesn't trigger new cert prompts.

**Apex and wildcard hosts.** Two reserved values let a single project answer for more than just specific subdomains:

| `host = ` | URL it serves | Use case |
|-----------|---------------|----------|
| `"@"`     | `https://{name}.localhost`   | the bare project domain |
| `"*"`     | catch-all for any `*.{name}.localhost` not claimed by a specific host | tenant subdomains, preview hosts |

Specific slugs win over wildcard (e.g. with both `host = "api"` and `host = "*"`, `api.foo.localhost` hits the `api` task and `anything-else.foo.localhost` hits the `*` task). At most one apex and one wildcard per project.

Linux: `sudo setcap cap_net_bind_service+ep $(readlink -f $(which python3))` lets the daemon bind `:443` without sudo at all (no privilege drop needed).

### How hostnames resolve

For browsers to reach `https://api.{project}.localhost`, the name has to resolve to 127.0.0.1. macOS doesn't resolve `*.localhost` natively, Windows doesn't either, and Linux is hit-or-miss. taskmux ships a pluggable resolver that runs once at daemon startup while still privileged:

| `host_resolver` | What it does |
|-----------------|--------------|
| `etc_hosts` (default) | Writes a managed block to `/etc/hosts` (or `%SystemRoot%\System32\drivers\etc\hosts` on Windows). Block is delimited by `# BEGIN taskmux managed` / `# END taskmux managed` and rewritten on every daemon start, so it's safe to coexist with your manual entries. **Static** — adding a new task host requires `sudo taskmux daemon` restart. |
| `dns_server` | Runs a tiny in-process DNS server on `127.0.0.1:5454` (5353 is mDNS — avoid) and delegates `.localhost` queries to it via `/etc/resolver/localhost` (macOS), a `systemd-resolved` drop-in (Linux), or NRPT (Windows). **Dynamic** — adding hosts at runtime is a pure in-memory update, no daemon restart, no privilege escalation. Catch-all: any unmapped `*.localhost` query also resolves to 127.0.0.1, matching RFC 6761. |
| `noop` | Don't touch anything. Use if you handle resolution yourself — a tunnel, custom DNS, dnsmasq, etc. |

The resolver is a small abstraction (`taskmux/host_resolver.py`) — adding a `CloudflareTunnelResolver`, `NgrokResolver`, or DDNS plugin later is a single class. Configure via `~/.taskmux/config.toml`:

```toml
host_resolver = "dns_server"      # "etc_hosts" | "dns_server" | "noop"
dns_server_port = 5454            # only used when host_resolver = "dns_server" (avoid 5353 = mDNS)
dns_managed_tld = "localhost"     # ditto
```

#### Switching to `dns_server`

```bash
# 1. set host_resolver = "dns_server" in ~/.taskmux/config.toml
# 2. start the daemon under sudo (needed to write /etc/resolver/<tld>);
#    the DNS server itself runs unprivileged after the install.
sudo taskmux daemon

# Manage delegation independently of daemon lifecycle:
taskmux dns install              # write /etc/resolver/localhost (sudo)
taskmux dns uninstall            # remove it
taskmux dns flush                # flush OS DNS cache
taskmux dns query api.foo.localhost   # debug: query our DNS server directly
```

With `etc_hosts`, hostnames added to a project after the daemon is running won't be auto-written (the daemon has dropped privileges) — restart `sudo taskmux daemon` to refresh the block. With `dns_server` this is a non-issue: new hosts are picked up immediately.

Disable / customize via `~/.taskmux/config.toml`:

```toml
proxy_enabled = true            # default
proxy_https_port = 443          # set to >=1024 (e.g. 8443) to run unprivileged — no sudo needed
proxy_bind = "127.0.0.1"        # loopback only by default — "0.0.0.0" exposes on LAN
```

`taskmux status` flips host-routed tasks to `healthy: false` when the proxy listener isn't bound or this project's host route isn't registered — see the top-level `proxy: {bound, port, reason}` block in `--json` output and the per-task `last_health.method == "proxy"` reason.

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
command = "python manage.py runserver 0.0.0.0:$PORT"
cwd = "apps/api"
host = "api"
depends_on = ["migrate"]
health_check = "curl -sf https://api.fullstack-app.localhost/health"
stop_grace_period = 10

[tasks.worker]
command = "celery -A myapp worker -l info"
cwd = "apps/api"
depends_on = ["db"]
restart_policy = "always"
max_restarts = 10
restart_backoff = 3.0

[tasks.web]
command = "bun dev --port $PORT"
cwd = "apps/web"
host = "web"
depends_on = ["api"]
health_check = "curl -sf https://web.fullstack-app.localhost"

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
| `auto_daemon` | `false` | when `true`, `taskmux start` also spawns a detached daemon (auto-restart + WS API) |
| `hooks.*` | — | `before_start`, `after_start`, `before_stop`, `after_stop` |
| **Task fields** | | |
| `command` | required | shell command to run |
| `auto_start` | `true` | include in `taskmux start` |
| `cwd` | — | working directory |
| `host` | — | DNS-safe subdomain (e.g. `"api"`), `"@"` for apex (`https://{name}.localhost`), or `"*"` for wildcard catch-all. When set, taskmux assigns a free port via `$PORT`, mints a wildcard cert for `*.{name}.localhost`, and routes `https://{host}.{name}.localhost` → that port |
| `host_path` | `"/"` | (reserved) base path for future health-URL auto-derivation |
| `health_url` | — | HTTP URL to probe (e.g. `http://localhost:8000/health`) — uses stdlib, no curl needed |
| `health_expected_status` | `200` | required HTTP status from `health_url` |
| `health_expected_body` | — | regex/substring; if set, response body must match (catches dev-server 200-with-error pages) |
| `health_check` | — | shell command (exit 0 = healthy) — used when `health_url` is unset |
| `health_interval` | `10` | seconds between checks |
| `health_timeout` | `5` | seconds before check times out |
| `health_retries` | `3` | consecutive failures before restart |
| `stop_grace_period` | `5` | seconds after C-c before SIGTERM |
| `restart_policy` | `"on-failure"` | `"no"`, `"on-failure"`, `"always"` |
| `max_restarts` | `5` | max restarts before giving up (resets after 60s healthy) |
| `restart_backoff` | `2.0` | exponential backoff base (capped 60s) |
| `log_file` | — | override log path (default: `~/.taskmux/projects/{session}/logs/{task}.log`) |
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

Probe precedence (first match wins):

1. **`health_url`** — HTTP GET via stdlib. Pass when status matches `health_expected_status` (default 200) and, if set, body matches `health_expected_body` (regex). No curl dependency.
2. **`health_check`** — arbitrary shell command, exit 0 = healthy.
3. **TCP probe** — when `host` is set, probes `localhost:$PORT` (the port taskmux assigned to the task). Pass when the port accepts a connection.
4. **fallback** — pane-alive check (foreground command is not a shell).

Must fail `health_retries` consecutive times before triggering restart.

### Why the body check matters

Many dev servers (Next.js, Vite, etc.) keep returning HTTP 200 even when the build is broken — they render the compile error as HTML. A `curl -sf` health check passes; the page is unusable. Pin a marker in `health_expected_body` to fail in that case:

```toml
[tasks.web]
command = "next dev -p $PORT"
host = "web"
health_url = "http://localhost:$PORT"
health_expected_body = "id=\"__next\""   # absent on the Next error overlay
```

Used by:
- `taskmux health` — status table (`-v` shows probe method + failure reason)
- `taskmux status` — surfaces the last failure under each unhealthy task
- `taskmux start` — dependency gating
- `start --monitor` / `daemon` — auto-restart trigger

## Daemon

A single global daemon manages every registered project on the host. Projects auto-register on `taskmux start`, and the daemon picks them up live via a registry watcher. Auto-restart only fires when the daemon (or `start --monitor`) is running.

```bash
taskmux start -d        # start tasks AND spawn the global daemon (auto-registers cwd)
taskmux daemon          # run foreground daemon (Ctrl+C to stop)
```

### Lifecycle

```bash
taskmux daemon start              # spawn detached global daemon (no-op if running)
taskmux daemon stop               # SIGTERM the daemon
taskmux daemon status             # running + pid + registered project count
taskmux daemon restart            # stop, wait for exit, respawn
taskmux daemon list               # all registered projects + live state
taskmux daemon register [-c PATH] # add cwd's (or PATH's) project to the registry
taskmux daemon register -f        # overwrite an existing entry whose config moved
taskmux daemon unregister NAME    # remove a project from the registry
```

`start`, `restart`, and `list` take `--port` to override the configured `api_port`; when omitted they fall back to `~/.taskmux/config.toml`. All commands accept `--json` (global flag). Daemon log: `~/.taskmux/daemon.log`.

Each project carries a `state`: `ok` while loaded, `config_missing` if its `taskmux.toml` is absent or was deleted (entry stays in the registry, health checks pause), `error` if loading the config raised. Surfaced in `daemon list` and the `list_projects` WS command.

If you move `taskmux.toml` to a new directory and re-register, the registry auto-heals when the old path no longer exists on disk. If both paths still exist, `register` rejects the collision (E305) — pass `--force` to make the new path win.

### WebSocket API

One port (default 8765). Messages carry a `session` field for per-project commands:

```json
{"command": "list_projects"}                                          // → {projects: [...]}
{"command": "status_all"}                                             // → aggregated
{"command": "status",  "params": {"session": "myapp"}}
{"command": "restart", "params": {"session": "myapp", "task": "web"}}
{"command": "kill",    "params": {"session": "myapp", "task": "web"}}
{"command": "logs",    "params": {"session": "myapp", "task": "web", "lines": 100}}
```

Unknown sessions return `{error: "unknown_session", session: "..."}`. Unknown commands return `{error: "unknown_command", command: "..."}`.

### Global config

Host-wide settings live at `~/.taskmux/config.toml`. Optional — every key has a default.

```toml
# ~/.taskmux/config.toml
health_check_interval = 30   # seconds; daemon health-check cadence
api_port              = 8765 # WebSocket API port
```

```bash
taskmux config show              # resolved view (defaults + overrides)
taskmux config set <key> <value> # writes the file (creates if absent); rejects unknown keys
taskmux config path              # print path
```

`config set` validates keys against the schema and rejects unknown ones with `E104` rather than silently dropping them. Daemon reads the file at startup. To pick up changes, `taskmux daemon restart`.

### Filesystem layout

```
~/.taskmux/
  config.toml                         # global host config (optional)
  daemon.pid                          # GLOBAL — single multi-project daemon
  daemon.log
  events.jsonl                        # global, cross-project event log
  registry.json                       # registered projects {session → config_path}
  projects/{session}/
    logs/{task}.log[.N]               # per-task output
```

`taskmux status` shows `Auto-restart: active (pid …)` when a daemon is detected, otherwise `Auto-restart: inactive` so you don't silently miss restarts. Set `auto_daemon = true` at the top of `taskmux.toml` to spawn one on every `taskmux start`.

## Persistent Logs

Task output is piped to `~/.taskmux/projects/{session}/logs/{task}.log` with UTC timestamps:

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
