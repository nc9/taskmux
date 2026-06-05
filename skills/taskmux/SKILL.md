---
name: taskmux
version: 0.9.10
description: Manage long-running dev tasks (servers, watchers, build processes) via taskmux — a daemon-supervised task runner driven by `taskmux.toml` with optional public exposure via Cloudflare Tunnel. TRIGGER when cwd has `taskmux.toml`, when the user mentions taskmux, or when the user wants to start/stop/inspect/tail-logs of long-running processes, or expose a service publicly via a tunnel. SKIP for one-shot commands (tests, builds) and for projects without `taskmux.toml`.
---

# taskmux

Long-running dev tasks (servers, watchers, builds) need persistent logs, auto-restart, and stable URLs — backgrounded shells (`npm run dev &`) give you none. Taskmux does.

Daemon-supervised task runner. Tasks are declared in `taskmux.toml`; the daemon spawns each as its own PTY-backed process group, mirrors output to a persistent timestamped log, and auto-restarts per `restart_policy`. Every command takes `--json`.

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
taskmux open <task>                 # open proxy URL in default browser (manual only)

# Logs (persistent, timestamped, ~/.taskmux/projects/{project_id}/logs/)
taskmux logs [<task>] [-f] [-n N] [-g PATTERN] [-C N] [--since 5m]
taskmux logs-clean [<task>]                 # alias for `clean --logs`

# Config
taskmux add <task> "<cmd>" [--host api]     # adds to taskmux.toml
taskmux remove <task>
taskmux inject [CLAUDE.md|AGENTS.md|all]    # refresh agent context block
                                            #   (or create file if missing)

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
taskmux daemon status               # pid + proxy/port binding (is :443/:80 bound?)
taskmux daemon register [--force]   # register cwd in global registry
sudo taskmux daemon install         # OS supervisor (launchd/systemd): auto-restart on crash/sleep/reboot
sudo taskmux daemon uninstall       # remove the OS supervisor

# Proxy / certs
taskmux ca install                  # one-time mkcert root install (OS keychain)
taskmux ca trust-clients            # trust CA in Node/Python (writes ~/.zshenv etc.)
taskmux ca trust-clients --print    # print exports without writing
taskmux ca trust-clients --shell zsh|bash|fish
taskmux dns install|uninstall|flush|query <name>

# Tunnels (public exposure — Cloudflare today, more providers coming)
taskmux tunnel enable [--backend cloudflare] [--token <t>] [--account-id <id>] \
                      [--zone <id>] [--task api --task web] \
                      [--public-hostname api=api.example.com] [--dry-run]
taskmux tunnel test                 # preflight: token, scopes, zones, DNS collisions
taskmux tunnel config [--reveal]    # cascaded view (global + project + sources)
taskmux tunnel config-set --scope global zone_id=abc api_token=cf-pat-...
taskmux tunnel disable [--prune]
taskmux tunnel status               # backend health, last sync, mappings
taskmux tunnel logs [cloudflare] [--follow]
```

## JSON patterns

```bash
# Status with proxy reachability
taskmux status --json | jq '{tasks: [.tasks[] | {name, state, url, last_health}], proxy}'

# Each task has a `state`: "running" | "starting" | "unhealthy" | "stopped".
# `starting` = process up but TCP port not yet bound, within boot_grace (default 10s).
# `unhealthy` = past boot_grace AND port not answering (or configured probe failing).
# Find anything not currently serving traffic:
taskmux status --json | jq '.tasks[] | select(.state != "running")'

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

**Privileged daemon (sudo) — do not replace it unprivileged.** With `proxy_https_port`/`proxy_http_redirect_port` <1024 or an `etc_hosts`/`dns_server` resolver, the daemon must bind those as root: run `sudo taskmux daemon`. It binds the privileged sockets as root, then drops to your user. Because it runs as your user afterward, a **non-root** `taskmux daemon start|restart` (or an auto-spawn triggered by any task command) would come up unprivileged and fail the fatal `:443` bind — breaking every `*.localhost` URL. taskmux now **refuses** those non-root (re)spawns with an actionable error instead of half-starting; re-run under `sudo`, or pass `--force` (sets `TASKMUX_ALLOW_UNPRIVILEGED=1`) to run unprivileged on purpose. To run unprivileged permanently, set `proxy_https_port >= 1024` (+ `proxy_http_redirect_port = 0`, `host_resolver = "noop"`) or `proxy_enabled = false`.

**Crash recovery — no duplicate processes.** The daemon persists each task's pid/pgid to `~/.taskmux/projects/<project>/state.json`. On macOS a hard daemon crash (SIGKILL/OOM) orphans tasks (they keep running); on restart the daemon reaps those orphaned process groups before respawning, so you don't end up with two copies of a task. Identity is an exact per-task `ps` marker injected into each task's command, so reaping only ever kills taskmux's own task that wrote the record. Implementation detail for command authors: each task command runs inside a subshell `( … )` under a marked `/bin/sh -c` leader — transparent to normal commands (`exec`, `&&`, heredocs, trailing comments all work).

## Public access (tunnels)

To expose a host-routed task on the public internet (webhooks, mobile, remote agents): the existing local URL is unaffected — public access is **additive**.

```bash
# Wizard / non-interactive — runs preflight, creates tunnel, writes DNS
taskmux tunnel enable --json \
    --token "$CLOUDFLARE_API_TOKEN" \
    --task api --public-hostname api=api.example.com \
    --task web --public-hostname web=web.example.com
```

Under `--json` (or non-TTY) every step is non-interactive. Missing inputs return `{"ok": false, "error": "missing_input", "field": "..."}` instead of prompting — feed them in via flags and re-run.

Cascade for config: `~/.taskmux/config.toml` `[tunnel.cloudflare]` (account_id, zone_id, api_token, tunnel_name) is the **default** for every project; `taskmux.toml` `[tunnel.cloudflare]` overrides per project; if zone_id is unset everywhere, it's auto-resolved from the public_hostname's apex.

Token policy:
- Lives in `~/.taskmux/config.toml` (`api_token` field, file chmod 0600 — daemon refuses otherwise) OR an env var named by `api_token_env` (default `CLOUDFLARE_API_TOKEN`).
- **Never** in `taskmux.toml` (git-tracked) — validation rejects it there.
- `taskmux config show` and `taskmux tunnel config` mask it; `--reveal` shows plaintext.

Per-task config (added by `tunnel enable`, but writable directly):
```toml
[tasks.api]
host = "api"
tunnel = "cloudflare"                   # also: "noop" for self-hosted infra
public_hostname = "api.example.com"
```

Validation rules: `tunnel` requires `host` set + non-wildcard + `public_hostname` valid FQDN.

Triage when something's off:
1. `taskmux tunnel test --json` — preflight without mutating. Missing piece is in the failed `check`.
2. `taskmux tunnel status --json` — `cloudflared` running? `last_error` text?
3. `taskmux tunnel logs cloudflare --follow` — child process stdout/stderr.
4. `taskmux url <task> --json` — local + public URL together.

When user says "expose this" / "make it public" / "tunnel": always default to `taskmux tunnel enable` against cwd. Don't manually edit Cloudflare — taskmux owns the cfd_tunnel, ingress, and DNS routes.

Provider note: only `cloudflare` is wired today. Tailscale Funnel and ngrok are deferred (single-funnel-per-node and free-tier-no-BYO-domain limits respectively). Self-hosted (frp / sish / Caddy) → set `tunnel = "noop"`; taskmux records the public hostname for status display while you wire exposure outside.

## MCP server (coding-agent integration)

The daemon hosts an MCP (Model Context Protocol) server at
`http://localhost:{api_port}/mcp/` (default `http://localhost:8765/mcp/`).
Streamable HTTP transport. Connected coding agents (Claude Code, Cursor,
Codex, OpenCode) get:

> **Prefer the MCP when it's loaded.** If `mcp__taskmux__*` tools appear
> in your tool list, reach for them before shelling out to `taskmux …`.
> The MCP tools return structured payloads, are scoped to one project via
> the `?session=` pin (cross-project calls return `pin_violation`), and
> stream `notifications/message` on task crashes / restarts / health
> failures so you don't have to poll. Use the CLI only when the MCP
> surface isn't available in your session.

  * tools — `taskmux_status`, `taskmux_inspect`, `taskmux_logs`,
    `taskmux_start/stop/restart/kill`, `taskmux_health`, `taskmux_events`,
    `taskmux_list_projects`
  * resources — `taskmux://status`, `taskmux://projects`,
    `taskmux://events/recent`, `taskmux://logs/{session}/{task}`
  * push notifications — `notifications/message` on every lifecycle event
    (severity mapped: `error` for crashes/health fails / max-restarts,
    `warning` for auto-restarts/kills, `info` otherwise)

### Connection scoping (per-project default)

The daemon is host-wide — one process serves every project on the host.
By default each agent's connection is **pinned to one project** via a
`?session=<name>` URL query param the installer writes into the agent's
config. Pinned connections see only their project's status, only their
project's events, and reject cross-project tool calls with
`{"error": "pin_violation"}`. `taskmux_list_projects` and
`taskmux://projects` stay global — pinned agents can still discover what
sibling projects exist.

Unpinned (admin) connections — installed with `--unscoped` — see every
project. Use these only for diagnostic / dotfiles-style clients.

### Wiring an agent

```bash
# From inside a project dir — auto-detects session from taskmux.toml.
# Omit the client name for an arrow-key checkbox prompt. Project-scoped
# targets (claude-project, cursor-project, codex-project) are pre-checked
# above a separator; user-global targets sit below as opt-in:
taskmux mcp install
#   ── project-scoped (recommended) ──
#   ● claude-project    <project>/.mcp.json
#   ● cursor-project    <project>/.cursor/mcp.json
#   ● codex-project     <project>/.codex/config.toml
#   ● opencode-project  <project>/opencode.json
#   ── user-global (host-wide, NOT recommended) ──
#   ○ claude            ~/.claude/settings.json
#   ○ cursor            ~/.cursor/mcp.json
#   ○ codex             ~/.codex/config.toml
#   ○ opencode          ~/.config/opencode/opencode.json

# Or specify directly (any of the above, or `all`):
taskmux mcp install claude-project
taskmux mcp install all

# Dry-run preview (full merged config)
taskmux mcp install --print

# Override cwd detection (install for a project from outside its dir)
taskmux mcp install --session myproj

# Host-wide, sees every project — admin only, emits a warning
taskmux mcp install --unscoped

# Snippet for copy-paste (same scope rules as install)
taskmux mcp show <client>

# Daemon endpoint, transport, active sessions (with pin), and a
# "this project" block showing the local URL + .mcp.json status
taskmux mcp status
```

For project-scoped MCP, prefer the `*-project` targets — they write to
files Claude Code (`.mcp.json`) and Codex CLI (`.codex/config.toml`)
load per-project, so the `?session=` pin is bound to the project rather
than leaking host-wide. Codex CLI's "closest wins" precedence resolves
the project entry inside trusted projects without disturbing user-global
servers (chrome-devtools, linear, etc.).

Running `taskmux mcp install` outside any taskmux project (no
`taskmux.toml` in cwd or any ancestor) errors with a hint — fail-closed
default keeps an agent's surface scoped to one project unless you opt out
explicitly. Restart the agent after install so it re-reads its config
and connects.

### Tuning ~/.taskmux/config.toml

```toml
[mcp]
enabled = true                          # default; set false to disable mounting
path = "/mcp"                           # default; only change on path collision
filter = []                             # default = every event;
                                        # quiet subset:
                                        # ["task_exited", "health_check_failed",
                                        #  "auto_restart", "task_killed"]
```

`enabled = false` keeps the daemon running on the legacy WS port without
exposing MCP. The durable `~/.taskmux/events.jsonl` log captures every
event regardless of the filter.

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

### "Daemon keeps dying / won't stay up"
- `taskmux daemon status` shows `running: false` repeatedly → it's an unsupervised foreground process (dies on crash/sleep/closed terminal/reboot). Install an OS supervisor: `sudo taskmux daemon install` (launchd on macOS, systemd on Linux). It auto-restarts on abnormal exit but honors a clean `taskmux daemon stop`.

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

### "Task hits `max_restarts_reached` repeatedly on a stale lock file"

Common with frameworks that protect their dev server with a file lock (Next.js
`.next/dev/lock`, Vite cache locks, Yarn/pnpm/Bun install locks, esbuild
service sockets). When taskmux SIGKILLs the previous instance — via
`taskmux kill`, the SIGINT→SIGTERM→SIGKILL escalation in `stop`, or
`_cleanup_port` reclaiming a contested port — the lock file is left behind.
The next spawn refuses to start, exits non-zero, and within 5 retries the
task hits `max_restarts` and parks.

Fix with a `before_start` hook — fires for every spawn (manual *and*
auto-restart), runs in the task's `cwd`:

```toml
[tasks.web]
command = "bun run dev"
cwd = "web"

[tasks.web.hooks]
before_start = "rm -f .next/dev/lock"     # Next.js
# before_start = "rm -f node_modules/.vite/deps/_metadata.json.lock"   # Vite
# before_start = "rm -f /tmp/esbuild-*.sock"                            # esbuild service
```

Diagnose with `taskmux logs <task> --grep "lock\\|EADDRINUSE\\|already in use"`
and `taskmux events --task <task>` — a `max_restarts_reached` cluster within
~30s of `task_started` is the smoking gun.

## Anti-patterns — DO NOT

- ❌ `npm run dev &` / `cargo watch ... &` — backgrounded shell processes have no logs, no restart, no visibility.
- ❌ `kill -9 <pid>` of a task process — bypasses manual-stop tracking; auto-restart re-spawns it. Use `taskmux stop`/`kill`.
- ❌ Hunting through terminal scrollback for log analysis — use `taskmux logs <task> --grep` against persistent logs.
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
auto_start = true           # false ⇒ never auto-started (not on daemon boot,
                            #   `start`, or `restart`); only explicit `start <task>`
cwd = "..."                 # relative to taskmux.toml dir
host = "api" | "@" | "*"    # → proxy URL; PORT exported into command only
tunnel = "cloudflare"       # optional: "cloudflare" | "noop"
public_hostname = "..."     # required when tunnel = "cloudflare"
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

| Command   | Signal (on task's process group)            | Auto-restart |
|-----------|---------------------------------------------|--------------|
| `stop`    | SIGINT → SIGTERM → SIGKILL (graceful)       | Blocked      |
| `kill`    | SIGKILL (immediate)                         | Blocked      |
| `restart` | Full stop + spawn fresh process             | Re-enabled   |

`stop`/`kill` set a manual-stop flag — restart policy suppressed until `start`/`restart` clears it.
