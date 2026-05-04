"""Tests for taskmux.mcp.install (config-snippet writers per client)."""

from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from taskmux.mcp.install import ALL_CLIENTS, install, installAll, serverUrl


def testServerUrlShape() -> None:
    assert serverUrl(8765) == "http://localhost:8765/mcp/"
    assert serverUrl(9999).endswith("/mcp/")
    assert serverUrl(8765, "/agent-mcp") == "http://localhost:8765/agent-mcp/"
    assert serverUrl(8765, session="taskmux") == "http://localhost:8765/mcp/?session=taskmux"
    assert (
        serverUrl(8765, "/agent-mcp", session="proj")
        == "http://localhost:8765/agent-mcp/?session=proj"
    )


def testInstallHonorsCustomMcpPath(tmp_path: Path, monkeypatch) -> None:
    """When `[mcp].path` is overridden, install must write the matching URL
    so clients hit the same endpoint the daemon serves at."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    install("claude", api_port=8765, mcp_path="/agent-mcp")

    target = tmp_path / ".claude" / "settings.json"
    body = json.loads(target.read_text())
    assert body["mcpServers"]["taskmux"]["url"] == "http://localhost:8765/agent-mcp/"


def testInstallWritesSessionPinnedUrl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    install("claude", api_port=8765, session="taskmux")

    target = tmp_path / ".claude" / "settings.json"
    body = json.loads(target.read_text())
    assert body["mcpServers"]["taskmux"] == {
        "type": "http",
        "url": "http://localhost:8765/mcp/?session=taskmux",
    }


def testInstallWritesTypeHttpForJsonClients(tmp_path: Path, monkeypatch) -> None:
    """Claude Code's `.mcp.json` schema requires `type: "http"` for HTTP MCPs."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    install("cursor", api_port=8765)

    body = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert body["mcpServers"]["taskmux"]["type"] == "http"


def testDetectSessionFromCwd(tmp_path: Path) -> None:
    from taskmux.mcp.install import detectSessionFromCwd

    project = tmp_path / "myproj"
    project.mkdir()
    (project / "taskmux.toml").write_text('name = "myproj"\n')

    # cwd at root
    assert detectSessionFromCwd(project) == "myproj"
    # cwd in nested subdir — walks up
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    assert detectSessionFromCwd(nested) == "myproj"


def testDetectSessionMissingReturnsNone(tmp_path: Path) -> None:
    from taskmux.mcp.install import detectSessionFromCwd

    empty = tmp_path / "empty"
    empty.mkdir()
    assert detectSessionFromCwd(empty) is None


def testInstallClaudeWritesJsonAtUserPath(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = install("claude", api_port=8765)

    target = tmp_path / ".claude" / "settings.json"
    assert result["path"] == str(target)
    assert result["wrote"] is True
    body = json.loads(target.read_text())
    assert body["mcpServers"]["taskmux"]["url"] == "http://localhost:8765/mcp/"


def testInstallCodexWritesTomlAtUserPath(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = install("codex", api_port=8765)

    target = tmp_path / ".codex" / "config.toml"
    assert result["path"] == str(target)
    doc = tomlkit.parse(target.read_text())
    assert doc["mcp_servers"]["taskmux"]["url"] == "http://localhost:8765/mcp/"  # type: ignore[index]


def testInstallCodexProjectWritesAtProjectRoot(tmp_path: Path, monkeypatch) -> None:
    """`codex-project` writes `<project>/.codex/config.toml` so the
    per-project pin coexists with user-global ~/.codex/config.toml.
    Codex CLI's "closest wins" precedence picks the project entry up
    inside trusted projects.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    project_root = tmp_path / "proj"
    project_root.mkdir()

    install("codex-project", api_port=8765, session="proj", cwd=project_root)

    target = project_root / ".codex" / "config.toml"
    assert target.exists()
    doc = tomlkit.parse(target.read_text())
    assert (
        doc["mcp_servers"]["taskmux"]["url"]  # type: ignore[index]
        == "http://localhost:8765/mcp/?session=proj"
    )
    # User-global ~/.codex/config.toml must NOT be touched.
    assert not (tmp_path / "fake-home" / ".codex" / "config.toml").exists()


def testInstallCodexProjectPreservesExistingTomlEntries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".codex").mkdir()
    (project_root / ".codex" / "config.toml").write_text(
        '# project-local codex config\n\n[mcp_servers.linear]\nurl = "https://mcp.linear.app/mcp"\n'
    )

    install("codex-project", api_port=8765, session="proj", cwd=project_root)

    text = (project_root / ".codex" / "config.toml").read_text()
    assert "# project-local codex config" in text
    assert "[mcp_servers.linear]" in text
    assert "[mcp_servers.taskmux]" in text


def testInstallPreservesOtherEntries(tmp_path: Path, monkeypatch) -> None:
    """An existing `mcpServers.other` entry must survive an install pass."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    target = tmp_path / ".cursor" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"mcpServers": {"other": {"url": "http://x"}}, "unrelated": "keep"})
    )

    install("cursor", api_port=8765)

    body = json.loads(target.read_text())
    assert body["mcpServers"]["other"]["url"] == "http://x"
    assert body["mcpServers"]["taskmux"]["url"] == "http://localhost:8765/mcp/"
    assert body["unrelated"] == "keep"


def testInstallPreservesCodexTomlFormatting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    target = tmp_path / ".codex" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text('# preserve me\n\n[mcp_servers.other]\nurl = "http://x"\n')

    install("codex", api_port=8765)

    text = target.read_text()
    assert "# preserve me" in text
    assert "[mcp_servers.other]" in text
    assert "[mcp_servers.taskmux]" in text


def testDryRunDoesNotTouchDisk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    target = tmp_path / ".claude" / "settings.json"
    result = install("claude", api_port=8765, write=False)

    assert result["wrote"] is False
    assert "rendered" in result
    assert not target.exists()


def testInstallProjectWritesToCwdMcpJson(tmp_path: Path) -> None:
    """`claude-project` target writes `<cwd>/.mcp.json` — the project-shared
    file Claude Code actually loads for MCP servers. Project-level
    `.claude/settings.json` is for hooks/permissions, not MCP."""
    project_root = tmp_path / "proj"
    project_root.mkdir()

    install("claude-project", api_port=8765, cwd=project_root)

    target = project_root / ".mcp.json"
    body = json.loads(target.read_text())
    assert body["mcpServers"]["taskmux"]["url"] == "http://localhost:8765/mcp/"
    # `.claude/settings.json` must NOT be touched by `claude-project`.
    assert not (project_root / ".claude" / "settings.json").exists()


def testInstallOpencodeProjectShape(tmp_path: Path, monkeypatch) -> None:
    """`opencode-project` writes `<project>/opencode.json` with OpenCode's
    distinct schema: top-level `mcp.<name>` (not `mcpServers`), entry
    shape `{type: "remote", url, enabled: true}`. The `$schema` pointer
    is set so OpenCode's editor tooling can validate.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    project_root = tmp_path / "proj"
    project_root.mkdir()

    install("opencode-project", api_port=8765, session="proj", cwd=project_root)

    target = project_root / "opencode.json"
    body = json.loads(target.read_text())
    assert body["$schema"] == "https://opencode.ai/config.json"
    assert body["mcp"]["taskmux"] == {
        "type": "remote",
        "url": "http://localhost:8765/mcp/?session=proj",
        "enabled": True,
    }
    # `mcpServers` (Claude/Cursor key) must NOT appear.
    assert "mcpServers" not in body


def testInstallOpencodeUserGlobalPath(tmp_path: Path, monkeypatch) -> None:
    """`opencode` writes `~/.config/opencode/opencode.json` (XDG-style),
    not a top-level `~/.opencode`."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    install("opencode", api_port=8765)

    target = tmp_path / ".config" / "opencode" / "opencode.json"
    assert target.exists()
    body = json.loads(target.read_text())
    assert body["mcp"]["taskmux"]["url"] == "http://localhost:8765/mcp/"


def testInstallCursorProjectWritesAtProjectRoot(tmp_path: Path, monkeypatch) -> None:
    """`cursor-project` writes `<project>/.cursor/mcp.json` — the
    project-scoped file Cursor's docs document. Same JSON schema as the
    user-global `~/.cursor/mcp.json`; project-level wins precedence.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
    project_root = tmp_path / "proj"
    project_root.mkdir()

    install("cursor-project", api_port=8765, session="proj", cwd=project_root)

    target = project_root / ".cursor" / "mcp.json"
    body = json.loads(target.read_text())
    assert body["mcpServers"]["taskmux"] == {
        "type": "http",
        "url": "http://localhost:8765/mcp/?session=proj",
    }
    # User-global ~/.cursor/mcp.json must NOT be touched.
    assert not (tmp_path / "fake-home" / ".cursor" / "mcp.json").exists()


def testDetectProjectRootFromCwdWalksAncestors(tmp_path: Path) -> None:
    """Anchor for `claude-project`: from a deep subdir, find the ancestor
    that contains `taskmux.toml`. Regression for the bug where running
    `taskmux mcp install` from `repo/src/` wrote `repo/src/.mcp.json`
    instead of `repo/.mcp.json`.
    """
    from taskmux.mcp.install import detectProjectRootFromCwd

    project = tmp_path / "myproj"
    project.mkdir()
    (project / "taskmux.toml").write_text('name = "myproj"\n')
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)

    assert detectProjectRootFromCwd(nested) == project.resolve()
    assert detectProjectRootFromCwd(project) == project.resolve()


def testDetectProjectRootMissingReturnsNone(tmp_path: Path) -> None:
    from taskmux.mcp.install import detectProjectRootFromCwd

    empty = tmp_path / "empty"
    empty.mkdir()
    assert detectProjectRootFromCwd(empty) is None


def testUnknownClientRaises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown client"):
        install("notreal", api_port=8765, write=False)


def testMalformedJsonRaisesAndDoesNotOverwrite(tmp_path: Path, monkeypatch) -> None:
    """An existing-but-unparsable settings.json must not be silently wiped.

    Regression test for review finding R-001: silent JSONDecodeError swallow
    let `mcp install` erase the user's other settings on the next write.
    """
    import json as _json

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    target = tmp_path / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    original = '{"trailing": "comma",}'  # invalid JSON
    target.write_text(original)

    import pytest

    with pytest.raises(_json.JSONDecodeError):
        install("claude", api_port=8765)

    # File must be untouched on disk.
    assert target.read_text() == original


def testInstallAllReportsPerClientErrorOnMalformedJson(tmp_path: Path, monkeypatch) -> None:
    """`installAll` collects per-client errors so one bad file doesn't sink
    the rest. The bad file is preserved as-is.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Pass cwd so project-scoped targets (claude-project, codex-project)
    # write under tmp_path and don't leak into the actual repo.
    monkeypatch.chdir(tmp_path)

    bad = tmp_path / ".cursor" / "mcp.json"
    bad.parent.mkdir(parents=True)
    original = '{"oops":'  # invalid
    bad.write_text(original)

    results = installAll(api_port=8765, cwd=tmp_path)

    by_client = {r["client"]: r for r in results}
    assert "error" in by_client["cursor"], by_client["cursor"]
    assert bad.read_text() == original
    # Other clients still get installed.
    assert by_client["claude"].get("wrote") is True


def testInstallAllReturnsOnePerClient(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    project_root = tmp_path / "proj"
    project_root.mkdir()

    results = installAll(api_port=8765, cwd=project_root)

    assert len(results) == len(ALL_CLIENTS)
    clients = {r["client"] for r in results}
    assert clients == set(ALL_CLIENTS)
    for r in results:
        assert "error" not in r, f"{r['client']}: {r}"
