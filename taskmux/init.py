"""Project initialization for Taskmux."""

from pathlib import Path

from .agent import detectInstalledAgents, injectAgentContext
from .config import CONFIG_FILENAME, configExists, writeConfig
from .models import TaskmuxConfig, slugify


def initProject(path: Path | None = None, defaults: bool = False) -> TaskmuxConfig:
    """Bootstrap a taskmux.toml config and inject agent context files."""
    project_path = path or Path.cwd()
    config_path = project_path / CONFIG_FILENAME

    if configExists(config_path):
        print(f"Config already exists: {config_path}")
        return TaskmuxConfig()

    # Determine session name (slugified to DNS-safe form for proxy URLs)
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

    # Agent detection + injection
    agents = detectInstalledAgents()

    if agents and not defaults:
        try:
            answer = input(f"Inject context for detected agents ({', '.join(agents)})? [Y/n]: ")
        except (EOFError, KeyboardInterrupt):
            print("\nSkipped agent injection.")
            return config
        if answer.strip().lower() in ("n", "no"):
            return config

    for agent in agents:
        target = injectAgentContext(agent, project_path, config)
        print(f"  Injected {agent} context -> {target.relative_to(project_path)}")

    if "claude" in agents and not defaults and not _skillInstalled(project_path):
        print(
            "  Tip: install the taskmux skill for richer Claude Code guidance:\n"
            "    npx skills add nc9/taskmux --skill taskmux"
        )

    return config


def _skillInstalled(project_path: Path) -> bool:
    """True if the taskmux skill is present at project or user scope."""
    project_skill = project_path / ".claude" / "skills" / "taskmux" / "SKILL.md"
    user_skill = Path.home() / ".claude" / "skills" / "taskmux" / "SKILL.md"
    return project_skill.exists() or user_skill.exists()
