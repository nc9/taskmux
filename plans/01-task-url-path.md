# Plan 01 — `path` field on `[tasks.X]` for fully-qualified URL exports

## Motivation

Today `taskmux env` emits base URLs only:

```
TASKMUX_URL_API=https://api.postpiece-feature-foo.localhost
```

Consumers that want a path-suffixed URL must derive it themselves in `.envrc`:

```bash
export API_BASE_URL="$TASKMUX_URL_API/api/v1"
```

That works but pushes the API path convention into every project's `.envrc`.
For projects with several path-bearing tasks the boilerplate adds up.

## Proposal

Optional `path` on `TaskConfig` — purely informational, only consumed by URL
emitters:

```toml
[tasks.api]
command = "bunx wrangler dev --port $PORT"
host = "api"
path = "/api/v1"
```

`taskmux env`, `taskmux url`, and `taskmux worktree urls` would render:

```
TASKMUX_URL_API=https://api.postpiece-feature-foo.localhost/api/v1
```

## Why NOT to overload `host_path`

`host_path` already exists on `TaskConfig` but it's the proxy router's path
prefix — a different concern. The proxy uses it to mount a task at a sub-path.
Conflating the two would force every project that wants a URL suffix to also
change proxy routing. Keep them separate.

## Surface area

- `taskmux/models.py` — add `path: str | None = None` to `TaskConfig`. Validate
  it starts with `/` if set.
- `taskmux/url.py` — add `taskUrlWithPath(project, host, path)` (already exists
  as `taskUrlPath`; check whether it's the right shape).
- `taskmux/env_export.py` — accept `tasks: list[tuple[str, str, str | None]]`
  with the optional path.
- `taskmux/cli.py` — pass `cfg.path` through `worktree_urls` and `env`.
- Tests: extend `test_env_export.py` and `test_url.py`.

## Backwards compat

Additive — defaults to `None`, behaviour unchanged when unset.

## Open questions

- Should `taskmux url <task>` print the path-suffixed URL, or stay
  base-only? Suggest path-suffixed by default with a `--no-path` flag for
  scripts that need the bare URL.
- Per-task aliasing — should we let a task declare which env var it maps to
  (`exports = "API_BASE_URL"`)? Probably overkill; `--prefix` already covers
  the common case.
