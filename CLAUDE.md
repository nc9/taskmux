# CLAUDE.md

## Project Overview

Taskmux is a tmux session manager written in Python that:
- Reads task definitions from `taskmux.toml` config files
- Manages tmux sessions/windows via libtmux
- Persistent timestamped logs via tmux `pipe-pane` at `~/.taskmux/logs/`
- `--json` flag on all CLI commands for programmatic consumption
- Event history at `~/.taskmux/events.jsonl` (starts, stops, health failures, restarts)
- Restart policies (`no`/`on-failure`/`always`) with health retries and backoff
- File watching for dynamic config reloading

## Architecture

### Core Components

- `taskmux/models.py` — Pydantic models (`TaskConfig`, `TaskmuxConfig`, `RestartPolicy`) with frozen immutability and unknown-key warnings
- `taskmux/config.py` — Functional TOML config: `loadConfig`, `writeConfig`, `addTask`, `removeTask`
- `taskmux/tmux_manager.py` — `TmuxManager` class: session/window ops, health checking, auto-restart, pipe-pane log attachment. `RestartTracker`: restart counts, health failures, manual-stop state. All action methods return result dicts (not print)
- `taskmux/cli.py` — Typer CLI with global `--json` callback. Commands: `start`, `stop`, `restart`, `kill`, `logs`, `logs-clean`, `add`, `remove`, `status`, `health`, `events`, `inspect`, `watch`, `daemon`
- `taskmux/daemon.py` — `TaskmuxDaemon` with watchdog file watcher, WebSocket API, health check loop
- `taskmux/output.py` — JSON output mode (`ContextVar`-based `is_json_mode()`, `print_result()`, `print_jsonl()`)
- `taskmux/events.py` — Event recording/querying: `recordEvent()` appends JSONL, `queryEvents()` reads/filters
- `taskmux/_log_pipe.py` — Stdin timestamper + rotator invoked by tmux `pipe-pane`

### Config Format

```toml
name = "session-name"

[tasks.server]
command = "echo 'Starting server...'"
restart_policy = "always"

[tasks.watcher]
command = "cargo watch -x check"
auto_start = false
restart_policy = "no"
```

- `auto_start` defaults to `true`, only written to file when `false`
- `restart_policy` defaults to `"on-failure"`, only written when `"no"` or `"always"`
- Config filename: `taskmux.toml`
- Uses `tomllib` (stdlib) for reading, `tomlkit` for writing (preserves formatting, `is_super_table=True` for `[tasks.*]`)

### Key Patterns

- **Functional config** — module-level functions, not classes. `loadConfig()` returns frozen `TaskmuxConfig`
- **Frozen models** — all config models are immutable. To modify, create new instance
- **`RestartPolicy` enum** — `"no"`, `"on-failure"` (default), `"always"`. Manual stops override all policies
- **`_StrictConfig` base** — shared by all models, warns on unknown keys
- **`_get_session()` helper** — asserts `self.session is not None` for type safety after `session_exists()` checks
- **Return dicts** — all TmuxManager action methods return `{"ok": True/False, ...}` dicts, CLI layer formats for human or JSON output
- **Persistent logs** — `pipe-pane` mirrors pane output through `_log_pipe.py` which timestamps + rotates
- **Event recording** — `recordEvent()` called at end of each action method, writes to `~/.taskmux/events.jsonl`

## Development

### Dependencies

Runtime: `libtmux`, `watchdog`, `typer`, `rich`, `pydantic`, `tomlkit`, `aiohttp`, `websockets`, `aiofiles`
Dev: `ruff`, `basedpyright`, `pytest`, `pytest-tmp-files`

### Build Commands

```
make dev      # uv sync
make test     # uv run pytest -v
make lint     # ruff check + basedpyright
make fmt      # ruff format + ruff check --fix
make check    # fmt → lint → test (full pipeline)
make link     # symlink to ~/.local/bin/taskmux
make clean    # rm dist/ build/ .pytest_cache/
make release  # check → bump → bump-skill → commit → tag → push → publish (BUMP=patch|minor|major)
make publish  # rm dist/ → uv build → uv publish (use ONLY if release commit/tag already exist)
```

### Releasing

Always release via `make release` — never hand-edit `pyproject.toml` / `uv.lock` /
`skills/taskmux/SKILL.md` versions. The target runs the full check pipeline,
bumps via `uv version --bump $(BUMP)`, syncs the skill frontmatter via
`bump-skill`, commits as `chore(release): vX.Y.Z`, tags, pushes, then publishes
to PyPI. Override the bump kind with `make release BUMP=minor` (default `patch`).

### Testing

- `pytest` with fixtures in `tests/conftest.py`
- `sample_toml` fixture writes a temp `taskmux.toml` for config tests
- `TmuxManager` tests mock `libtmux.Server` to avoid tmux dependency
- CLI tests use `typer.testing.CliRunner` with mocked config functions
- Type checking: `basedpyright` in `standard` mode, Python 3.11+

### Code Style

- Python 3.11+ (enables `tomllib` stdlib)
- Ruff: `line-length = 100`, select `E,F,I,UP,B,SIM`
- Type hints on all function signatures
- Functional config module, class-based tmux manager and CLI

<!-- taskmux:start -->
# Taskmux — taskmux

## Tasks

| Task | URL | Auto-start | Command |
|------|-----|------------|---------|
| foo | — | yes | `--json` |

Always use taskmux to manage long-running processes (servers, watchers, queues) instead of running them directly. If the `taskmux` skill is installed, prefer it for CLI details. Otherwise: `taskmux --help`, `taskmux status --json`, `taskmux inspect <task> --json`, `taskmux logs <task> --grep <pat>`.
<!-- taskmux:end -->
