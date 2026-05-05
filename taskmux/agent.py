"""Agent context-file injection.

Targets the universal `CLAUDE.md` (Claude Code) and `AGENTS.md` (Codex,
OpenCode, and most other agent CLIs) at the project root. Idempotent —
the marked block is replaced in place on subsequent `taskmux init` runs.
"""

import re
import shutil
from pathlib import Path

from .models import TaskmuxConfig

CONTEXT_START = "<!-- taskmux:start -->"
CONTEXT_END = "<!-- taskmux:end -->"

CLAUDE_FILE = "CLAUDE.md"
AGENTS_FILE = "AGENTS.md"
CONTEXT_FILES = (CLAUDE_FILE, AGENTS_FILE)

# Soft signal — used only to phrase prompts ("we see you have claude").
# Injection itself targets CLAUDE.md / AGENTS.md regardless.
KNOWN_AGENT_BINARIES = ("claude", "codex", "opencode")

SKILL_INSTALL_CMD = "npx skills add nc9/taskmux --skill taskmux -g"


def detectInstalledAgents() -> list[str]:
    """Return agent CLI binaries on PATH (soft signal for prompts)."""
    return [b for b in KNOWN_AGENT_BINARIES if shutil.which(b)]


def detectContextFiles(project_path: Path) -> list[Path]:
    """Return existing CLAUDE.md / AGENTS.md at the project root."""
    return [project_path / name for name in CONTEXT_FILES if (project_path / name).exists()]


_SKILL_NAME = "taskmux"

# Per-agent install paths used by `vercel-labs/skills` (`npx skills add`).
# Project-local entries are joined to the project root; global entries are
# joined to $HOME. `.agents/skills` is the shared convention used by Codex,
# OpenCode, Cursor, Gemini CLI, Copilot, Cline, Warp, etc.
_PROJECT_SKILL_DIRS = (
    Path(".claude") / "skills",  # Claude Code
    Path(".agents") / "skills",  # shared cross-agent
    Path(".codex") / "skills",
    Path(".opencode") / "skills",
)
_GLOBAL_SKILL_DIRS = (
    Path(".claude") / "skills",
    Path(".agents") / "skills",
    Path(".codex") / "skills",
    Path(".config") / "opencode" / "skills",
    Path(".config") / "agents" / "skills",
)


def skillInstalled(project_path: Path | None = None) -> bool:
    """True if the taskmux skill is reachable at any known agent skill path.

    Checks `<project>/<dir>/taskmux/SKILL.md` for each dir in
    `_PROJECT_SKILL_DIRS`, plus `~/<dir>/taskmux/SKILL.md` for each dir in
    `_GLOBAL_SKILL_DIRS`. Covers the install targets advertised by
    `npx skills add` (https://github.com/vercel-labs/skills).
    """
    home = Path.home()
    candidates = [home / d / _SKILL_NAME / "SKILL.md" for d in _GLOBAL_SKILL_DIRS]
    if project_path is not None:
        candidates.extend(project_path / d / _SKILL_NAME / "SKILL.md" for d in _PROJECT_SKILL_DIRS)
    return any(p.exists() for p in candidates)


def buildContextBlock(config: TaskmuxConfig) -> str:
    """Render the marker-delimited taskmux block patched into agent context files.

    Pointer-only: no per-task table. The agent should probe
    `mcp__taskmux__taskmux_status` (preferred) or `taskmux status --json`
    (fallback) for live task state — that data goes stale the moment a
    task is added/removed and an in-flight agent already past its
    system-prompt load won't re-read this file. Keeping the block
    short keeps token cost flat regardless of project size.
    """
    lines = [
        CONTEXT_START,
        f"# Taskmux — {config.name}",
        "",
        "This project uses **taskmux** for long-running processes (dev servers, "
        "watchers, queues). Don't run those directly (`bun dev &`, `cargo "
        "watch`) — always go through taskmux so logs, restarts, and proxied "
        "URLs work.",
        "",
        "**Probe live state — don't rely on this file.** Tasks change; this "
        "block won't always reflect that.",
        "",
        "  * If `mcp__taskmux__*` tools are loaded → prefer them "
        "(`taskmux_status`, `taskmux_inspect`, `taskmux_logs`, "
        "`taskmux_events`). Structured payloads, scoped to this project via "
        "the `?session=` pin, plus push notifications on crashes / restarts.",
        "  * Otherwise CLI: `taskmux status --json`, "
        "`taskmux inspect <task> --json`, `taskmux logs <task> --grep <pat>`. "
        "`taskmux --help` for the full surface; install the `taskmux` skill "
        "for cross-agent guidance.",
        "",
        "If the MCP isn't wired up yet, run `taskmux mcp install` from this dir.",
        CONTEXT_END,
    ]
    return "\n".join(lines) + "\n"


def reinjectIfEnabled(project_path: Path, config: TaskmuxConfig) -> list[Path]:
    """Re-patch existing CLAUDE.md / AGENTS.md after a task add/remove.

    Honors `config.auto_inject_agents` (per-project override) falling back to
    the global `auto_inject_agents` knob. Only updates files that already
    exist — first-time creation belongs to `taskmux init`. Best-effort: any
    error is swallowed so a write failure never blocks the originating CLI op.

    Returns the list of paths actually rewritten (empty when disabled or no
    context files exist).
    """
    if config.auto_inject_agents is False:
        return []
    if config.auto_inject_agents is None:
        try:
            from .global_config import loadGlobalConfig

            if not loadGlobalConfig().auto_inject_agents:
                return []
        except Exception:  # noqa: BLE001
            return []
    written: list[Path] = []
    for target in detectContextFiles(project_path):
        try:
            injectIntoFile(target, config)
            written.append(target)
        except OSError:
            continue
    return written


def injectIntoFile(target: Path, config: TaskmuxConfig) -> Path:
    """Write or update the marked taskmux block in `target`. Creates parent dirs.

    Returns the absolute path written.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    block = buildContextBlock(config)

    if not target.exists():
        target.write_text(block)
        return target

    content = target.read_text()
    pattern = re.compile(
        re.escape(CONTEXT_START) + r".*?" + re.escape(CONTEXT_END),
        re.DOTALL,
    )
    if pattern.search(content):
        content = pattern.sub(block.rstrip("\n"), content)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + block
    target.write_text(content)
    return target
