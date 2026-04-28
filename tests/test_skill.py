"""Sanity check for the bundled Claude Code skill at skills/taskmux/SKILL.md."""

from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).parent.parent / "skills" / "taskmux" / "SKILL.md"


@pytest.fixture(scope="module")
def skill_text() -> str:
    assert SKILL_PATH.exists(), f"missing skill file: {SKILL_PATH}"
    return SKILL_PATH.read_text()


def test_skill_has_frontmatter(skill_text: str):
    assert skill_text.startswith("---\n"), "skill must start with YAML frontmatter"
    end = skill_text.find("\n---\n", 4)
    assert end != -1, "skill must close its frontmatter with ---"


def test_skill_name_and_description(skill_text: str):
    head = skill_text.split("\n---\n", 2)[0]
    assert "name: taskmux" in head
    assert "description:" in head
    desc_line = next(line for line in head.splitlines() if line.startswith("description:"))
    assert len(desc_line) > len("description: ") + 20, "description must be non-trivial"


def test_skill_mentions_core_commands(skill_text: str):
    for cmd in ("taskmux start", "taskmux status", "taskmux logs", "taskmux inspect"):
        assert cmd in skill_text, f"skill body should reference `{cmd}`"


def test_skill_mentions_json_flag(skill_text: str):
    assert "--json" in skill_text
