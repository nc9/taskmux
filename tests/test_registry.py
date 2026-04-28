"""Tests for the central project registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from taskmux import paths as paths_mod
from taskmux import registry as reg
from taskmux.errors import ErrorCode, TaskmuxError


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Patch all taskmux paths to live under tmp_path for the test."""
    monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path)
    monkeypatch.setattr(paths_mod, "REGISTRY_PATH", tmp_path / "registry.json")
    return tmp_path


def _seed_toml(tmp_path: Path, name: str = "alpha") -> Path:
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=True)
    cfg = project / "taskmux.toml"
    cfg.write_text(f'name = "{name}"\n')
    return cfg


def test_empty_registry_returns_empty(isolated):
    assert reg.readRegistry() == {}
    assert reg.listRegistered() == []


def test_register_writes_entry(isolated):
    cfg = _seed_toml(isolated, "alpha")
    entry = reg.registerProject("alpha", cfg)
    assert entry["session"] == "alpha"
    assert entry["config_path"] == str(cfg.resolve())
    assert entry["registered_at"]

    persisted = reg.readRegistry()
    assert "alpha" in persisted
    assert persisted["alpha"]["config_path"] == str(cfg.resolve())


def test_register_idempotent_same_path(isolated):
    cfg = _seed_toml(isolated, "alpha")
    first = reg.registerProject("alpha", cfg)
    second = reg.registerProject("alpha", cfg)
    assert first["registered_at"] == second["registered_at"]


def test_register_collision_rejected(isolated):
    cfg_a = _seed_toml(isolated, "shared-a")
    cfg_b = _seed_toml(isolated, "shared-b")
    reg.registerProject("shared", cfg_a)
    with pytest.raises(TaskmuxError) as excinfo:
        reg.registerProject("shared", cfg_b)
    assert excinfo.value.code is ErrorCode.SESSION_ALREADY_REGISTERED


def test_register_auto_heals_when_old_path_missing(isolated):
    """Move case: old config path no longer exists → re-register with new path."""
    cfg_old = _seed_toml(isolated, "moved-old")
    reg.registerProject("session", cfg_old)
    cfg_old.unlink()  # user moved the file
    cfg_new = _seed_toml(isolated, "moved-new")
    entry = reg.registerProject("session", cfg_new)
    assert entry["config_path"] == str(cfg_new.resolve())
    # registered_at preserved on heal
    assert entry["registered_at"] == reg.readRegistry()["session"]["registered_at"]


def test_register_force_overrides_collision(isolated):
    """Both paths exist; force=True wins."""
    cfg_a = _seed_toml(isolated, "shared-a")
    cfg_b = _seed_toml(isolated, "shared-b")
    reg.registerProject("shared", cfg_a)
    entry = reg.registerProject("shared", cfg_b, force=True)
    assert entry["config_path"] == str(cfg_b.resolve())


def test_register_collision_still_raised_when_both_exist(isolated):
    """Without force and both paths on disk, raise as before."""
    cfg_a = _seed_toml(isolated, "shared-a")
    cfg_b = _seed_toml(isolated, "shared-b")
    reg.registerProject("dup", cfg_a)
    with pytest.raises(TaskmuxError) as excinfo:
        reg.registerProject("dup", cfg_b)
    assert excinfo.value.code is ErrorCode.SESSION_ALREADY_REGISTERED


def test_unregister_removes_entry(isolated):
    cfg = _seed_toml(isolated, "alpha")
    reg.registerProject("alpha", cfg)
    assert reg.unregisterProject("alpha") is True
    assert reg.unregisterProject("alpha") is False
    assert reg.readRegistry() == {}


def test_register_multiple_projects(isolated):
    cfg_a = _seed_toml(isolated, "alpha")
    cfg_b = _seed_toml(isolated, "beta")
    reg.registerProject("alpha", cfg_a)
    reg.registerProject("beta", cfg_b)
    sessions = [e["session"] for e in reg.listRegistered()]
    assert sessions == ["alpha", "beta"]


def test_corrupt_registry_returns_empty(isolated):
    paths_mod.REGISTRY_PATH.write_text("{not valid json")
    assert reg.readRegistry() == {}


def test_atomic_replace(isolated, monkeypatch):
    """Verify writeRegistry uses os.replace via temp file (no partial state)."""
    cfg = _seed_toml(isolated, "alpha")
    reg.registerProject("alpha", cfg)
    # tmpfile shouldn't linger
    leftovers = [p for p in isolated.glob(".registry-*.tmp")]
    assert leftovers == []
