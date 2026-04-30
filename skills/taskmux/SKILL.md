---
name: taskmux
version: 0.7.0
description: Manage long-running dev tasks (servers, watchers, build processes) via taskmux — a tmux-backed task runner driven by `taskmux.toml`. TRIGGER when cwd has `taskmux.toml`, when the user mentions taskmux, or when the user wants to start/stop/inspect/tail-logs of long-running processes in a project that has taskmux configured. SKIP for one-shot commands (tests, builds) and for projects without `taskmux.toml`.
---

# taskmux

Long-running dev tasks (servers, watchers, builds) need persistent logs, auto-restart, and stable URLs — backgrounded shells (`npm run dev &`) give you none. Taskmux does.

Tmux-backed task runner. Tasks are declared in `taskmux.toml`; taskmux runs each in its own tmux window, mirrors output to a persistent timestamped log, and (when a daemon or `--monitor` is up) auto-restarts per `restart_policy`. Every command takes `--json`.

## When to invoke

- `taskmux.toml` exists in repo root → use taskmux for any long-running process. Do NOT run those commands directly.
- User says "start the server", "tail logs", "what's running", "restart the watcher", "is X healthy" in a taskmux project.
- User asks to add/remove a task, or to debug a crash/restart loop.

Skip for: one-shot commands (`pytest`, `bun build`, `cargo test`), projects without `taskmux.toml`.

## Detection

```bash
test -f taskmux.toml && taskmux --json status
```

If `running: false`, start tasks first.

## CLI cheat sheet

`--json` works on every command.

```bash
# Lifecycle
taskmux start [<task>...]           # all auto_start, dep-ordered; or specific
taskmux start -m                    # + monitor (auto-restart, foreground)
taskmux start -d                    # + spawn detached daemon
taskmux stop [<task>...]            # graceful: C-c → SIGTERM → SIGKILL
taskmux restart [<task>...]         # full stop + start, clears manual-stop
taskmux kill <task>                 # SIGKILL + destroy window (blocks restart)

# Inspect
taskmux status                      # overview (aliases: list, ls)
taskmux health                      # health-check table (-v for probe details)
taskmux inspect <task>              # full task state
taskmux events [--task X --since 1h]
taskmux url <task>                  # print proxy URL

# Logs (persistent, timestamped, ~/.taskmux/projects/{project_id}/logs/)
taskmux logs [<task>] [-f] [-n N] [-g PATTERN] [-C N] [--since 5m]
taskmux logs-clean [<task>]                 # alias for `clean --logs`

# Config
taskmux add <task> "<cmd>" [--host api]     # adds to taskmux.toml
taskmux remove <task>

# Aliases — proxy a non-taskmux port (Docker, external dev server)
taskmux alias add <name> <port> [--host h]  # https://h.{project}.localhost
taskmux alias list
taskmux alias remove <name>

# Cleanup
taskmux clean                       # current project: logs+state+certs+registry
taskmux clean --logs|--events|--certs|--all [--dry-run] [--yes] [--force]
taskmux prune                       # report orphans (stray sessions, leaked ports)
taskmux prune --apply               # actually clean

# Daemon (auto-restart + WebSocket API on api_port)
taskmux daemon [start|stop|status|restart|list]
taskmux daemon register [--force]   # register cwd in global registry

# Proxy / certs
taskmux ca install                  # one-time mkcert root install (OS keychain)
taskmux ca trust-clients            # trust CA in Node/Python (writes ~/.zshenv etc.)
taskmux ca trust-clients --print    # print exports without writing
taskmux ca trust-clients --shell zsh|bash|fish
taskmux dns install|uninstall|flush|query <name>
```

## JSON patterns

```bash
# Status with proxy reachability
taskmux status --json | jq '{tasks: [.tasks[] | {name, healthy, url, last_health}], proxy}'

# Find unhealthy (proxy-down counts here too — last_health.method == "proxy")
taskmux status --json | jq '.tasks[] | select(.healthy == false)'

# Why did a task fail?
taskmux inspect <task> --json
taskmux events --task <task> --since 1h --json | jq '.events[-5:]'
taskmux logs <task> --since 10m -g "error|exception|fatal"

# Recent restarts
taskmux events --since 1h --json | jq '.events[] | select(.event | test("auto_restart|max_restarts_reached"))'
```

Result shape: `{"ok": true|false, ...}`. Error: `{"ok": false, "error": "..."}`.

## Hosts (HTTPS proxy)

`host` on a task exposes it via the daemon's HTTPS proxy. Three forms:

| `host` value | URL | Notes |
|--------------|-----|-------|
| `"api"` (slug) | `https://api.{project}.localhost` | most common |
| `"@"` (apex)   | `https://{project}.localhost` | one per project |
| `"*"` (wildcard) | catch-all for `*.{project}.localhost` | one per project; URL displayed as `https://*.{project}.localhost` (display only) |

Slug + apex + wildcard can coexist; specific hosts win over wildcard. Duplicate slugs/apex/wildcard rejected at config-validation time.

### Trusting the CA in Node/Python (the `unable to verify the first certificate` gotcha)

`mkcert -install` (run by `taskmux ca install`) only trusts the root in the **OS keychain**. Node.js (Claude Code, Cursor, VS Code, Electron, MCP SDKs) and Python (`requests`/`httpx`/`ssl`) ignore the keychain — they need env vars pointing at a CA bundle file. Symptom: HTTPS calls to `https://*.{project}.localhost` fail with `UNABLE_TO_VERIFY_LEAF_SIGNATURE` / `unable to verify the first certificate` / `SSL: CERTIFICATE_VERIFY_FAILED`.

Fix:

```bash
taskmux ca install         # OS keychain (browsers, curl)
taskmux ca trust-clients   # writes NODE_EXTRA_CA_CERTS / REQUESTS_CA_BUNDLE / SSL_CERT_FILE
                           #   to ~/.zshenv (zsh) / ~/.bashrc (bash) / ~/.config/fish/config.fish
source ~/.zshenv           # apply in current shell (new shells inherit automatically)
```

`trust-clients` builds a **combined bundle** at `~/.taskmux/ca-bundle.pem` containing the system CAs (Mozilla roots) + mkcert local CA, then points the env vars at it. This is critical: setting `SSL_CERT_FILE` to mkcert's single-CA `rootCA.pem` would strand openssl-using tools (npm, curl, bun publish) and break public TLS with `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`. Re-run `trust-clients` after mkcert reissues the root or system CAs change.

Idempotent — re-running replaces the managed sentinel block in place. `--print` emits exports to stdout for `eval` / dotfile managers; `--shell <zsh|bash|fish>` overrides `$SHELL`.

How proxy serves a request:
1. Daemon binds `:443` (configurable) and a per-process `state.json` records each task's `$PORT`.
2. On `taskmux start`, a task with `host = "api"` registers its assigned port; `taskmux alias add` registers external ports the same way.
3. Browser hits `https://api.{project}.localhost` → daemon SNI-matches the cert → proxies to `127.0.0.1:$PORT`.

### `$PORT` injection — the gotcha that breaks host routing

Taskmux assigns each task a random port and exports `PORT=<n>` **only into the task's command**. The dev command MUST listen on `$PORT` or the proxy routes to a port nothing is bound to (TCP probe → "connect refused", browser → 502).

- ✅ `vite dev --port ${PORT:-9000}` / `next dev -p $PORT` / `os.environ.get("PORT", default)`
- ❌ `vite dev --port 9000` (hardcoded — proxy can't reach it)
- ❌ `port = 9000` in `taskmux.toml` — **rejected** (`E103: Unknown config key 'port'`); ports are dynamic in 0.7+. Migrate by deleting the key and updating the dev command to read `$PORT`.
- ❌ `$PORT` in `health_url` / `health_check` — **NOT substituted**, passed verbatim. For host-routed tasks, just omit health config and taskmux TCP-probes the assigned port automatically.

The daemon's proxy listener is configurable via `~/.taskmux/config.toml`:

```toml
proxy_enabled = true
proxy_https_port = 443             # change to >=1024 to run unprivileged
proxy_bind = "127.0.0.1"           # "0.0.0.0" exposes to LAN (be deliberate)
```

`taskmux status` flips host-routed tasks to `healthy: false` when the proxy listener isn't bound or this project's host route isn't registered. The reason is in `last_health.reason`; the top-level `proxy: {bound, port, reason}` summarises overall state.

## Worktrees

Linked git worktrees get an auto-suffixed `project_id` (e.g. `myproject-feat-foo`) so logs, registry entries, and proxy URLs (`https://api.myproject-feat-foo.localhost`) don't collide with the primary checkout. The user-facing `name` in `taskmux.toml` stays the same; everything routed by `project_id` namespaces automatically.

## Common workflows

### "Server died, what happened?"
1. `taskmux status --json` → confirm task state + `proxy.bound`.
2. `taskmux events --task <task> --since 1h --json` → `health_check_failed`, `auto_restart`, `max_restarts_reached`.
3. `taskmux logs <task> --since 30m -g "error|panic|exception"` → root cause.
4. `taskmux inspect <task> --json` → restart count, last failure.

### "URL says healthy but my browser can't reach it"
- Check `taskmux status --json | jq .proxy` — `bound: false` means daemon proxy isn't listening (run `sudo taskmux daemon` or set `proxy_https_port` >=1024).
- Check `last_health.method == "proxy"` on the task — reason text says exactly which gate failed.

### "Add a new dev task"
```bash
taskmux add worker "celery -A app worker -l info"
taskmux add api "next dev -p $PORT" --host api    # adds proxy URL
taskmux start worker api
```

### "Change restart behavior"
Edit `taskmux.toml`:
```toml
[tasks.worker]
restart_policy = "always"          # "no" | "on-failure" (default) | "always"
max_restarts = 10
restart_backoff = 3.0
```
Daemon picks up changes via file watcher.

## Anti-patterns — DO NOT

- ❌ `npm run dev &` / `cargo watch ... &` — backgrounded shell processes have no logs, no restart, no visibility.
- ❌ `kill -9 <pid>` of a taskmux pane — bypasses manual-stop tracking; auto-restart re-spawns it. Use `taskmux stop`/`kill`.
- ❌ `tmux capture-pane` for log analysis — use `taskmux logs <task> --grep` against persistent logs.
- ❌ Editing `taskmux.toml` without a daemon running — file watcher only runs in `taskmux daemon` or `start --monitor`.
- ❌ `taskmux init` on a project that already has `taskmux.toml` — no-op.

## Filesystem

```
~/.taskmux/
  projects/{project_id}/logs/{task}.log[.N]   # rotated, timestamped
  projects/{project_id}/state.json            # per-project assigned ports
  projects/{project_id}/aliases.json          # external proxy routes (taskmux alias)
  events.jsonl                                # cross-project lifecycle
  registry.json                               # daemon-managed projects
  certs/{project_id}/                         # minted *.localhost certs
  daemon.pid, daemon.log
  config.toml                                 # global host config (preserved by `clean --all`)
```

`{project_id}` includes the worktree suffix on linked worktrees.

## Config quick-ref (top-level)

```toml
name = "myproject"
auto_start = true
auto_daemon = false

[tasks.<name>]
command = "..."
auto_start = true
cwd = "..."                 # relative to taskmux.toml dir
host = "api" | "@" | "*"    # → proxy URL; PORT exported into command only
depends_on = []
# Health: omit when `host` is set — taskmux TCP-probes assigned port.
# $PORT is NOT substituted in these fields (passed verbatim to http/shell).
health_url = "http://localhost:8080/health"
health_expected_status = 200
health_expected_body = "..."   # regex; catches "200 with error page"
health_check = "..."           # shell, exit 0 = healthy
health_interval = 10
health_retries = 3
restart_policy = "on-failure"
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

`stop`/`kill` set a manual-stop flag — restart policy suppressed until `start`/`restart` clears it.
