"""Agent detection and context injection for AI coding tools."""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .models import TaskmuxConfig

CONTEXT_START = "<!-- taskmux:start -->"
CONTEXT_END = "<!-- taskmux:end -->"


@dataclass(frozen=True)
class AgentDef:
    binary: str
    context_file: str


KNOWN_AGENTS: dict[str, AgentDef] = {
    "claude": AgentDef(binary="claude", context_file=".claude/rules/taskmux.md"),
    "codex": AgentDef(binary="codex", context_file="AGENTS.md"),
    "opencode": AgentDef(binary="opencode", context_file="AGENTS.md"),
}


def detectInstalledAgents() -> list[str]:
    """Return names of agents whose binaries are on PATH."""
    return [name for name, defn in KNOWN_AGENTS.items() if shutil.which(defn.binary)]


def buildContextBlock(config: TaskmuxConfig) -> str:
    """Build a markdown context block describing the taskmux setup."""
    lines = [
        CONTEXT_START,
        f"# Taskmux — {config.name}",
        "",
        "## Tasks",
        "",
    ]

    if config.tasks:
        for name, task in config.tasks.items():
            auto = "" if task.auto_start else " (manual)"
            lines.append(f"- **{name}**: `{task.command}`{auto}")
    else:
        lines.append('_No tasks configured yet. Use `taskmux add <name> "<command>"` to add._')

    lines.extend(
        [
            "",
            "## Usage",
            "",
            "```bash",
            "taskmux start              # Start all auto_start tasks",
            "taskmux stop               # Stop all tasks",
            "taskmux stop <task>        # Graceful stop (C-c) a single task",
            "taskmux start <task>       # Start a single task",
            "taskmux restart <task>     # Restart a single task",
            "taskmux logs <task>        # Show recent logs",
            'taskmux logs <task> --grep "error"  # Search logs',
            "taskmux inspect <task>     # JSON task state",
            "taskmux status             # Session overview",
            "```",
            "",
            "Always use taskmux to manage long-running processes instead of running them directly.",
            CONTEXT_END,
        ]
    )
    return "\n".join(lines) + "\n"


def injectAgentContext(agent_name: str, project_path: Path, config: TaskmuxConfig) -> Path:
    """Write or update context block in an agent's context file. Returns path written."""
    defn = KNOWN_AGENTS[agent_name]
    target = project_path / defn.context_file
    target.parent.mkdir(parents=True, exist_ok=True)

    block = buildContextBlock(config)

    if target.exists():
        content = target.read_text()
        # Replace existing block
        pattern = re.compile(
            re.escape(CONTEXT_START) + r".*?" + re.escape(CONTEXT_END),
            re.DOTALL,
        )
        if pattern.search(content):
            content = pattern.sub(block.rstrip("\n"), content)
        else:
            # Append
            if not content.endswith("\n"):
                content += "\n"
            content += "\n" + block
        target.write_text(content)
    else:
        target.write_text(block)

    return target
