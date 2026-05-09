# Plan 02 — `taskmux worktree init` / `taskmux worktree teardown`

## Motivation

When wiring Claude Code worktree support into PostPiece we ended up writing
two project-side scripts:

- `scripts/worktree-init.ts` — read `taskmux worktree status --json`, write
  per-worktree env, `bun install`, `taskmux start --if-stopped`.
- `scripts/worktree-teardown.ts` — confirm + `taskmux stop`, optional clean,
  refuse on primary.

Most of the logic is generic: identity detection, env emission (now handled by
`taskmux env`), running a per-project bootstrap command, idempotent start /
stop, primary-checkout safety. Every project that adopts the same pattern
will write the same shell glue.

## Proposal

Two opinionated wrappers, driven by a new `[worktree]` table in
`taskmux.toml`:

```toml
[worktree]
init_command = "bun install"
teardown_confirm = true   # default: true on primary, false on linked worktrees
teardown_clean = false    # also wipe per-project state on teardown
```

Then:

- `taskmux worktree init` — runs `init_command` if `node_modules`/etc. is
  stale, then `taskmux start --if-stopped`. `--auto` flag exits silently
  when not in a linked worktree (mirrors the SessionStart hook gate).
- `taskmux worktree teardown` — refuses on primary unless `--force`,
  prompts unless `--yes`, runs `taskmux stop` and (if configured)
  `taskmux clean`.

Project's `.claude/settings.json` collapses to:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [{"type": "command", "command": "taskmux worktree init --auto"}]
      }
    ],
    "WorktreeRemove": [
      {"hooks": [{"type": "command", "command": "taskmux worktree teardown --yes"}]}
    ]
  }
}
```

No project-side scripts needed.

## Why NOT generic project-side hooks

We considered exposing arbitrary `before_init` / `after_teardown` hooks but
that quickly becomes a parallel hook surface to Claude Code's own. Better to
keep taskmux's role narrow: identity + start/stop semantics. The `init_command`
is the one escape hatch — it's where projects put things taskmux doesn't
know about (deps install, db migrations, etc.).

## Surface area

- `taskmux/models.py` — `WorktreeLifecycleConfig` with `init_command`,
  `teardown_confirm`, `teardown_clean`.
- `taskmux/cli.py` — two new subcommands under `worktree_app`:
  `worktree init` and `worktree teardown`. Both call into a shared helper
  that owns the safety gates.
- `taskmux/worktree.py` (or new `taskmux/lifecycle.py`) — pure functions for
  the decision flow (when to run init_command, when to skip, etc.).
- Tests: end-to-end CLI invocations with mocked subprocess + ipc.
- Docs: README + skills/taskmux/SKILL.md update.

## Backwards compat

Additive. Projects without `[worktree]` get sensible defaults
(`init_command = None` → no-op; `teardown_confirm = true`).

## Open questions

- **Primary checkout behaviour** — should `worktree init` do anything in the
  primary, or is it strictly a worktree-only command? Suggest: in primary,
  `--auto` is a no-op; manual run prints a hint and exits 0.
- **Multiple init commands** — `init_commands = ["bun install", "bun run db:migrate"]`?
  Probably yes, list form. Single string for the common case is also OK
  (parse as `[cmd]`).
- **Teardown vs Claude Code's WorktreeRemove** — Claude already removes
  worktrees on clean exit. `worktree teardown` only stops taskmux; the
  worktree directory + branch removal stays with Claude (or `git worktree
  remove`). Document this clearly so users don't expect it to delete
  the worktree.
- **Daemon coupling** — should teardown also signal the daemon to drop the
  project from its registry, or leave that to `taskmux clean`? Suggest:
  drop-from-registry is part of the default teardown; full state wipe stays
  behind `--clean`.
