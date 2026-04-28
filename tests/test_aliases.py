"""Tests for aliases.py — per-project alias map."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskmux import aliases as aliases_mod
from taskmux import paths
from taskmux.errors import TaskmuxError


@pytest.fixture(autouse=True)
def _isolate_taskmux_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = tmp_path / "taskmux"
    monkeypatch.setattr(paths, "TASKMUX_DIR", fake)
    monkeypatch.setattr(paths, "PROJECTS_DIR", fake / "projects")
    fake.mkdir(parents=True, exist_ok=True)
    yield fake


def test_add_and_load_round_trip():
    aliases_mod.addAlias("demo", None, "db", 5432)
    aliases_mod.addAlias("demo", None, "cache", 6379, host="redis")
    out = aliases_mod.loadAliases("demo", None)
    assert out["db"] == {"host": "db", "port": 5432}
    assert out["cache"] == {"host": "redis", "port": 6379}


def test_remove_drops_entry_and_file_when_empty():
    aliases_mod.addAlias("demo", None, "db", 5432)
    assert aliases_mod.removeAlias("demo", None, "db") is True
    assert aliases_mod.loadAliases("demo", None) == {}
    assert not paths.projectAliasesPath("demo", None).exists()


def test_remove_returns_false_when_missing():
    assert aliases_mod.removeAlias("demo", None, "ghost") is False


def test_lookup():
    aliases_mod.addAlias("demo", None, "db", 5432)
    entry = aliases_mod.lookupAlias("demo", None, "db")
    assert entry is not None
    assert entry["host"] == "db"
    assert aliases_mod.lookupAlias("demo", None, "ghost") is None


def test_rejects_dotted_name():
    with pytest.raises(TaskmuxError):
        aliases_mod.addAlias("demo", None, "with.dot", 5000)


def test_rejects_reserved_host():
    with pytest.raises(TaskmuxError):
        aliases_mod.addAlias("demo", None, "x", 5000, host="*")
    with pytest.raises(TaskmuxError):
        aliases_mod.addAlias("demo", None, "x", 5000, host="@")


def test_rejects_out_of_range_port():
    with pytest.raises(TaskmuxError):
        aliases_mod.addAlias("demo", None, "db", 0)
    with pytest.raises(TaskmuxError):
        aliases_mod.addAlias("demo", None, "db", 99999)


def test_worktree_isolation():
    aliases_mod.addAlias("demo", None, "db", 5432)
    aliases_mod.addAlias("demo", "feat-x", "db", 5433)
    assert aliases_mod.loadAliases("demo", None)["db"]["port"] == 5432
    assert aliases_mod.loadAliases("demo", "feat-x")["db"]["port"] == 5433


def test_rejects_duplicate_host():
    aliases_mod.addAlias("demo", None, "db", 5432)
    with pytest.raises(TaskmuxError):
        aliases_mod.addAlias("demo", None, "db2", 6000, host="db")


def test_update_same_alias_keeps_host():
    aliases_mod.addAlias("demo", None, "db", 5432)
    entry = aliases_mod.addAlias("demo", None, "db", 5433)
    assert entry["port"] == 5433
    assert aliases_mod.loadAliases("demo", None)["db"]["port"] == 5433
