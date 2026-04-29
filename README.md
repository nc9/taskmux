<p align="center">
  <img alt="taskmux" src="assets/taskmux-wordmark-light-800.webp" width="320">
</p>

# Taskmux

Task manager for coding agents with dependencies, observability, health monitoring, local hostnames, worktree support, tunnel support and a lot more. 

Example `taskmux.toml` for your project:

```toml
name = "example"

[tasks.api]
command = "uv run api --port $PORT" # taskmux injects a free $PORT per task
cwd = "api"                         # working directory
host = "api"                        # binds https://api.example.localhost (mkcert trusted)
                                    # for host-routed tasks, taskmux TCP-probes $PORT — no health_url needed
env = { LOG_LEVEL = "debug" }       # extra env vars merged into the task

[tasks.website]
command = "npm run dev -- --port $PORT"  # framework reads $PORT, taskmux routes the proxy
cwd = "web"
host = "@"                          # apex: https://example.localhost (one per project)
depends_on = ["api"]                # waits for api's health_url to pass before starting
restart_policy = "always"           # respawn on crash with exponential backoff

[tasks.worker]
command = "uv run worker"
depends_on = ["api"]
restart_policy = "on-failure"       # default; only restart on non-zero exit
max_restarts = 10

[tasks.db]
command = "docker compose up postgres"
auto_start = false                  # skipped by `taskmux start`; run with `taskmux start db`
health_check = "pg_isready -h localhost -p 5432"  # shell exit-0 == healthy
stop_grace_period = 15              # seconds after SIGINT before SIGTERM

# Or expose any external/Docker port through the same proxy without a task:
#   taskmux alias add admin 8080 --host admin    # -> https://admin.example.localhost
#   taskmux tunnel up website                    # public URL via Cloudflare Tunnel
```

then in shell:

```bash
$ taskmux start
$ taskmux restart website
$ taskmux logs --since 5m website
```

etc. all with JSON outputs for agents and skills that allow you to add / edit in your favourite coding agent:

```bash
> add our website service to taskmux and bind it to apex domain.
```

A live `taskmux status` for the example project above:

```
$ taskmux status

  Status   Task     URL                            Public                                 Command                          Notes
  Healthy  api      https://api.example.localhost  —                                      uv run api --port $PORT          cwd=api
  Healthy  website  https://example.localhost      https://example-web.trycloudflare.com  npm run dev -- --port $PORT      cwd=web deps=[api] restart=always
  Healthy  worker   —                              —                                      uv run worker                    deps=[api] restart-max=10
  Stopped  db       —                              —                                      docker compose up postgres       manual

Aliases (external routes):
  Name   URL                              Target
  admin  https://admin.example.localhost  127.0.0.1:8080
```

`taskmux status --json` returns the same data with `tunnel.public_url`, `last_health`, `pid`, `port`, and event counters per task.

## Features

### No port juggling

- **Dynamic `$PORT` injection** — taskmux picks a free port per task and exports `$PORT` into the command. Configs never pin ports, so two checkouts (or two worktrees) of the same project never collide.
- **HTTPS proxy with trusted certs** — `host = "api"` exposes a task at `https://api.{project}.localhost` with an mkcert-signed wildcard cert. Apex (`@` → `https://{project}.localhost`) and wildcard (`*`, catch-all) routes coexist with specific subdomains.
- **Aliases for non-taskmux ports** — `taskmux alias add admin 8080 --host admin` routes `https://admin.{project}.localhost` to any external/Docker/sidecar port without declaring it as a task.
- **Dynamic DNS server** — optional in-process resolver answers any `*.localhost` to `127.0.0.1`, so adding hosts requires no `/etc/hosts` churn and no daemon restart. Falls back to `etc_hosts` or `noop` if you'd rather manage resolution yourself.
- **Cloudflare Tunnel support** — `taskmux tunnel up <task>` exposes a host-routed task at a public HTTPS URL via Cloudflare; per-task auth, cascade config, and tunnel state tracked alongside the task.
- **Port cleanup** — orphaned listeners on assigned ports are killed before a task starts, so a crashed previous run never blocks the next one.

### Worktree-aware (parallel agents, no collisions)

- Linked git worktrees auto-namespace their `project_id` (`myproject-feat-foo`) so logs, state, registry entries, and proxy URLs (`https://api.myproject-feat-foo.localhost`) don't collide with the primary checkout — or with another agent running in a sibling worktree.
- The user-facing `name` in `taskmux.toml` stays the same; everything routed by `project_id` namespaces automatically. Spawn N parallel agents on N branches, each gets its own URL and log directory.

### Agent-native observability

- **`--json` on every command** — machine-readable output for programmatic consumption; agents parse `status`, `inspect`, `events`, `logs` directly.
- **Persistent timestamped logs** — survive task kill / daemon restart at `~/.taskmux/projects/{project_id}/logs/`. Rotated by size; greppable with `taskmux logs -g <pat> -C N --since 5m`.
- **Event history** — lifecycle events (start, stop, health failure, auto-restart, max-restarts-reached) appended to `~/.taskmux/events.jsonl`. Filter by task / time / event type.
- **Health checks** — `health_url` (HTTP probe), `health_check` (shell exit code), or auto TCP probe for host-routed tasks. Retries gate dependents and trigger auto-restart.
- **Agent context injection** — `taskmux init` patches a thin task table + pointer into the project's `CLAUDE.md` / `AGENTS.md`; re-rendered on every `taskmux add` / `remove` so agents never see a stale list. Pair with the [taskmux skill](#agent-skill) for cross-agent CLI guidance.

### Process supervision

- **Task orchestration** — start/stop/restart with dependency ordering and graceful signal escalation (`SIGINT` → `SIGTERM` → `SIGKILL` on the task's process group).
- **Restart policies** — `no`, `on-failure` (default), `always` with exponential backoff and a `max_restarts` ceiling that resets after a healthy interval.
- **Lifecycle hooks** — `before_start`, `after_start`, `before_stop`, `after_stop` at global and per-task scope. Run shell commands or scripts at every state transition.
- **Daemon-owned processes** — every task runs under the daemon as its own process group on a PTY (colors + `isatty()` keep working). Daemon shutdown signal-cascades into every task; CLI commands are thin RPC over a local WebSocket.

## Install

Requires Python 3.11+. No tmux dependency.

```bash
uv tool install taskmux
```

### Agent skill

Generic CLI guidance (when to invoke, JSON patterns, anti-patterns) lives in a portable agent skill at [`skills/taskmux/`](skills/taskmux/SKILL.md). It works with Claude Code, Codex, OpenCode, Cursor, Gemini CLI, Copilot, and any other agent that speaks the [`vercel-labs/skills`](https://github.com/vercel-labs/skills) convention. Project task tables still come from `taskmux init` patching `CLAUDE.md` / `AGENTS.md`.

Install via:

```bash
npx skills add nc9/taskmux --skill taskmux        # project-local (.claude/skills/ or .agents/skills/)
npx skills add nc9/taskmux --skill taskmux -g     # global (~/.claude/skills/, ~/.agents/skills/, etc.)
```

`taskmux init` checks for the skill at the common install paths and prints the install hint if it's missing — you only need to run it once per machine (or per project for project-local installs).

### Agent context files (`taskmux init`)

`taskmux init` patches a small marked block (`<!-- taskmux:start --> ... <!-- taskmux:end -->`) into the project's existing `CLAUDE.md` and/or `AGENTS.md`. The block lists current tasks + URLs and points the agent at `taskmux --json status` / `inspect` / `logs`.

- Both files exist → patches both.
- One file exists → patches that one.
- Neither exists → interactive prompt asks which to create (default: `AGENTS.md`, the cross-agent convention). With `--defaults`, creates `AGENTS.md`.
- Re-running `taskmux init` (after `taskmux add`/`remove`) replaces the marked block in place — your other notes in the file are untouched.

The block also re-renders automatically on every `taskmux add` / `taskmux remove`, so the agent context never drifts from the live task list. Disable per-project in `taskmux.toml`:

```toml
auto_inject_agents = false
```

…or globally in `~/.taskmux/config.toml` (project setting wins when both are present):

```toml
auto_inject_agents = false
```

Or via `taskmux config set auto_inject_agents false`.

## Commands

All commands support `--json` for machine-readable output.

```bash
# Lifecycle
taskmux start                    # start all auto_start tasks in dependency order
taskmux start <task> [task2...]  # start specific tasks
taskmux stop                     # graceful stop all (SIGINT → SIGTERM → SIGKILL on process group)
taskmux stop <task> [task2...]   # stop specific tasks
taskmux restart                  # restart all
taskmux restart <task>           # restart specific tasks
taskmux kill <task>              # hard-kill (SIGKILL on the task's process group)

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
taskmux logs-clean [task]        # delete log files (alias for `clean --logs`)

# Config
taskmux add <task> "<command>"   # add task to taskmux.toml
taskmux add api "next dev" --host api  # expose at https://api.{project}.localhost
taskmux remove <task>            # remove task (kills if running)
taskmux init                     # create taskmux.toml + inject agent context
taskmux init --defaults          # non-interactive

# URLs / proxy
taskmux url <name>               # print proxy URL for a task or alias
taskmux ca install               # install local CA into system trust store (one-time)
taskmux ca mint                  # mint cert for the current project

# Aliases — proxy a non-taskmux port (Docker, external dev server)
taskmux alias add db 5432        # → https://db.{project}.localhost
taskmux alias add cache 6379 --host redis
taskmux alias list
taskmux alias remove db

# Cleanup
taskmux clean                    # current project: logs + state + certs + registry
taskmux clean --logs             # logs only
taskmux clean --events           # truncate events.jsonl
taskmux clean --certs            # remove minted *.localhost certs
taskmux clean --all              # wipe ~/.taskmux/ except config.toml
taskmux clean --dry-run          # report only, no deletes
taskmux prune                    # report orphans (stray sessions, leaked ports)
taskmux prune --apply            # actually clean up

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
                              # Everything after the bind (task processes,
                              # certs, state) runs as you, not root.
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

### Public access (Cloudflare Tunnel)

Local-only by default. To expose any host-routed task on the public internet — for webhooks, mobile testing, or remote agents — run the wizard:

```bash
brew install cloudflared
taskmux tunnel enable
```

That's it. The wizard prompts for a Cloudflare API token, your account ID, picks the zone, sets up the tunnel, writes DNS, and updates `taskmux.toml`. Re-running is idempotent. Non-interactive callers (agents, CI):

```bash
taskmux tunnel enable --json \
    --token "$CLOUDFLARE_API_TOKEN" \
    --task api --public-hostname api=api.example.com \
    --task web --public-hostname web=web.example.com
```

Once set up, the local URL is unchanged: `https://api.myproject.localhost` still works. The public hostname is **additive** — `taskmux status` shows it in a separate `Public URL` column for tunneled tasks.

#### Cascading config

`~/.taskmux/config.toml` holds the credentials shared by every project. Per-project `taskmux.toml` overrides any field one at a time. If `zone_id` is unset everywhere, taskmux auto-resolves it from the public hostname's apex.

```toml
# ~/.taskmux/config.toml — host-wide defaults (chmod 0600 if api_token embedded)
[tunnel.cloudflare]
account_id = "abcd..."
zone_id    = "ef56..."
api_token  = "cf-pat-..."     # OR api_token_env = "CLOUDFLARE_API_TOKEN"
```

```toml
# taskmux.toml — per-project (zone_id/tunnel_name optional, no token here)
[tunnel.cloudflare]
zone_id = "ghij..."           # only if this project uses a different zone

[tasks.api]
command = "bun run dev"
host = "api"
tunnel = "cloudflare"
public_hostname = "api.example.com"
```

Token scopes required: `Account → Cloudflare Tunnel → Edit`, `Zone → DNS → Edit`.

#### Daily commands

```bash
taskmux tunnel test              # preflight (token, scopes, zones, DNS collisions)
taskmux tunnel config            # cascaded view + per-field source
taskmux tunnel config-set --scope global zone_id=abc account_id=xyz
taskmux tunnel status            # backend health, last sync, mappings
taskmux tunnel logs cloudflare   # tail the cloudflared child process
taskmux tunnel disable [--prune] # strip tunnel fields from every task
```

Every command takes `--json` and emits a stable schema for agent scripting.

#### Safety rails

- API token in `~/.taskmux/config.toml` is masked in `config show` and `tunnel config` (use `--reveal` to show plaintext).
- Daemon refuses to read an embedded token if the file is wider than 0600.
- `api_token` cannot be set in `taskmux.toml` (git-tracked) — validation rejects it.
- DNS collision check refuses to overwrite an existing record at the public hostname unless it already points at this tunnel.
- Missing `cloudflared` binary, missing token, missing zone, and missing scope all surface as preflight check failures with concrete `fix:` hints.

If anything is missing the daemon logs the gap and disables the cloudflare backend — tunneled tasks still serve locally. Apex hosts (`host = "@"`) tunnel to `<project>.localhost`. Wildcard hosts (`host = "*"`) cannot be tunneled — there's no single FQDN to point at.

> **Tailscale Funnel** and **ngrok** are deferred for now. Tailscale Funnel is one funnel per node and limits the public URL to your tailnet; ngrok's free tier blocks BYO domains. For self-hosted tunnels (frp, sish, Caddy), set `tunnel = "noop"` on a task — taskmux records the public hostname for display and you wire the actual exposure outside.

### stop vs kill vs restart

| Command | Signal | Auto-restart |
|---------|--------|--------------|
| `stop` | SIGINT → SIGTERM → SIGKILL on the task's process group | Blocked |
| `kill` | SIGKILL on the task's process group | Blocked |
| `restart` | Full stop + spawn fresh process | Re-enabled |

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
| `name` | `"taskmux"` | project / session name (DNS-safe; used in proxy URLs) |
| `auto_start` | `true` | global toggle — `false` registers project but launches nothing |
| `auto_daemon` | `false` | (legacy) the daemon now starts implicitly on `taskmux start` |
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
| `stop_grace_period` | `5` | seconds after SIGINT before SIGTERM |
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
4. **fallback** — process-alive check (the daemon's tracked subprocess hasn't exited).

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

A single global daemon owns every task process on the host (docker-style — daemon shutdown signal-cascades into all tasks). The CLI is a thin client that auto-spawns the daemon on first use and auto-registers the cwd's project; the daemon picks projects up live via a registry watcher.

```bash
taskmux start           # auto-spawns the daemon if needed, then RPCs in
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

`taskmux status` shows `Auto-restart: active (pid …)` when a daemon is detected.

## Persistent Logs

The daemon attaches a PTY to each task and drains it line-by-line into `~/.taskmux/projects/{session}/logs/{task}.log` with UTC timestamps:

```
2024-01-01T14:00:00.123 Server started on port 3000
2024-01-01T14:00:01.456 GET /health 200 2ms
```

Logs survive task restart and daemon shutdown. Rotated at `log_max_size` (default 10MB), keeping `log_max_files` (default 3). Filter with `--since`:

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

## Architecture

```
┌────────────┐  WebSocket   ┌──────────────────────────────────┐
│ taskmux …  │ ───────────▶ │ taskmux daemon                   │
│ (CLI       │              │  ├─ Supervisor[project A]        │
│  client)   │ ◀─────────── │  │   ├─ task: api  (PTY + setsid)│
└────────────┘   one-shot   │  │   └─ task: web  (PTY + setsid)│
                  RPC       │  ├─ Supervisor[project B] …      │
                            │  ├─ HTTPS proxy on :443          │
                            │  └─ optional in-process DNS      │
                            └──────────────────────────────────┘
```

Each task runs as a child of the daemon, in its own process group, with a PTY attached so `isatty()` keeps returning true and ANSI colors survive into log files. `taskmux daemon stop` (or any clean SIGTERM) signal-cascades into every task's process group.

## Links

- [PyPI](https://pypi.org/project/taskmux/)
- [GitHub](https://github.com/nc9/taskmux)

## License

MIT
