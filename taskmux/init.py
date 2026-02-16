"""Project initialization for Taskmux."""

from pathlib import Path

from .agent import detectInstalledAgents, injectAgentContext
from .config import CONFIG_FILENAME, configExists, writeConfig
from .models import TaskmuxConfig


def initProject(path: Path | None = None, defaults: bool = False) -> TaskmuxConfig:
    """Bootstrap a taskmux.toml config and inject agent context files."""
    project_path = path or Path.cwd()
    config_path = project_path / CONFIG_FILENAME

    if configExists(config_path):
        print(f"Config already exists: {config_path}")
        return TaskmuxConfig()

    # Determine session name
    dir_name = project_path.name or "taskmux"

    if defaults:
        session_name = dir_name
    else:
        try:
            answer = input(f"Session name [{dir_name}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return TaskmuxConfig()
        session_name = answer or dir_name

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

    return config
