"""Project initialization for Taskmux."""

from pathlib import Path

from .agent import (
    AGENTS_FILE,
    CLAUDE_FILE,
    SKILL_INSTALL_CMD,
    detectContextFiles,
    detectInstalledAgents,
    injectIntoFile,
    skillInstalled,
)
from .config import CONFIG_FILENAME, configExists, writeConfig
from .models import TaskmuxConfig, slugify


def initProject(path: Path | None = None, defaults: bool = False) -> TaskmuxConfig:
    """Bootstrap a taskmux.toml config and patch agent context files."""
    project_path = path or Path.cwd()
    config_path = project_path / CONFIG_FILENAME

    if configExists(config_path):
        print(f"Config already exists: {config_path}")
        return TaskmuxConfig()

    dir_name = slugify(project_path.name or "taskmux")

    if defaults:
        session_name = dir_name
    else:
        try:
            answer = input(f"Session name [{dir_name}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return TaskmuxConfig()
        session_name = slugify(answer) if answer else dir_name

    config = TaskmuxConfig(name=session_name)
    writeConfig(config_path, config)
    print(f"Created {config_path}")

    targets = _resolveContextTargets(project_path, defaults=defaults)
    for target in targets:
        injectIntoFile(target, config)
        print(f"  Patched {target.relative_to(project_path)}")

    if not skillInstalled(project_path):
        agents = detectInstalledAgents()
        scope = "agent CLIs" if agents else "Claude Code / Codex / OpenCode"
        print(
            f"  Tip: install the taskmux skill for richer {scope} guidance:\n"
            f"    {SKILL_INSTALL_CMD}\n"
            f"    (drop -g for project-local install under .claude/skills/)"
        )

    return config


def _resolveContextTargets(project_path: Path, *, defaults: bool) -> list[Path]:
    """Pick which context files to patch.

    Existing CLAUDE.md/AGENTS.md → patch them. Neither present → in
    interactive mode, ask which to create; in --defaults mode, default
    to AGENTS.md (the cross-agent convention).
    """
    existing = detectContextFiles(project_path)
    if existing:
        return existing

    if defaults:
        return [project_path / AGENTS_FILE]

    print("\nNo CLAUDE.md or AGENTS.md found. Where should taskmux usage notes go?")
    print("  [1] AGENTS.md  (Codex, OpenCode, most agent CLIs — recommended)")
    print("  [2] CLAUDE.md  (Claude Code)")
    print("  [3] both")
    print("  [s] skip")
    try:
        choice = input("Choose [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nSkipped agent injection.")
        return []
    if choice in ("s", "skip"):
        return []
    if choice == "2":
        return [project_path / CLAUDE_FILE]
    if choice == "3":
        return [project_path / CLAUDE_FILE, project_path / AGENTS_FILE]
    return [project_path / AGENTS_FILE]
