"""Tests for shell rc file mutation in shell_env.py."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from taskmux import shell_env
from taskmux.errors import ErrorCode, TaskmuxError


@pytest.fixture
def tmpHome(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_detect_shell_from_env(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert shell_env.detectShell(None) == "zsh"


def test_detect_shell_override_wins(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/zsh")
    assert shell_env.detectShell("fish") == "fish"


def test_detect_shell_unknown_raises(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/tcsh")
    with pytest.raises(TaskmuxError) as e:
        shell_env.detectShell(None)
    assert e.value.code == ErrorCode.INVALID_ARGUMENT


def test_detect_shell_unset_raises(monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    with pytest.raises(TaskmuxError):
        shell_env.detectShell(None)


def test_rc_path_for_each_shell(tmpHome: Path):
    assert shell_env.rcPathFor("zsh") == tmpHome / ".zshenv"
    assert shell_env.rcPathFor("bash") == tmpHome / ".bashrc"
    assert shell_env.rcPathFor("fish") == tmpHome / ".config" / "fish" / "config.fish"


def test_render_exports_only_zsh():
    out = shell_env.renderExportsOnly(Path("/x/rootCA.pem"), "zsh")
    assert out == (
        'export NODE_EXTRA_CA_CERTS="/x/rootCA.pem"\n'
        'export REQUESTS_CA_BUNDLE="/x/rootCA.pem"\n'
        'export SSL_CERT_FILE="/x/rootCA.pem"\n'
    )


def test_render_exports_only_bash_quotes_spaces():
    out = shell_env.renderExportsOnly(Path("/has space/rootCA.pem"), "bash")
    assert 'export NODE_EXTRA_CA_CERTS="/has space/rootCA.pem"\n' in out


def test_render_exports_only_fish():
    out = shell_env.renderExportsOnly(Path("/x/rootCA.pem"), "fish")
    assert out == (
        'set -gx NODE_EXTRA_CA_CERTS "/x/rootCA.pem"\n'
        'set -gx REQUESTS_CA_BUNDLE "/x/rootCA.pem"\n'
        'set -gx SSL_CERT_FILE "/x/rootCA.pem"\n'
    )


def test_render_block_has_sentinels():
    out = shell_env.renderBlock(Path("/x/rootCA.pem"), "zsh")
    assert out.startswith("# >>> taskmux trust-clients >>>")
    assert out.endswith("# <<< taskmux trust-clients <<<")
    assert "Managed by" in out


def test_apply_creates_missing_rc(tmp_path: Path):
    rc = tmp_path / ".zshenv"
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "zsh", rcOverride=rc)
    assert res["ok"] is True
    assert res["action"] == "wrote"
    assert rc.exists()
    text = rc.read_text()
    assert "NODE_EXTRA_CA_CERTS" in text
    assert "# >>> taskmux trust-clients >>>" in text
    assert "# <<< taskmux trust-clients <<<" in text


def test_apply_creates_parent_dir_for_fish(tmp_path: Path):
    rc = tmp_path / ".config" / "fish" / "config.fish"
    assert not rc.parent.exists()
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "fish", rcOverride=rc)
    assert res["action"] == "wrote"
    assert rc.exists()
    assert "set -gx NODE_EXTRA_CA_CERTS" in rc.read_text()


def test_apply_appends_when_no_block_present(tmp_path: Path):
    rc = tmp_path / ".bashrc"
    rc.write_text("export PATH=/foo:$PATH\nalias ll='ls -la'\n")
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "bash", rcOverride=rc)
    assert res["action"] == "wrote"
    text = rc.read_text()
    assert text.startswith("export PATH=/foo:$PATH\n")
    assert "alias ll='ls -la'" in text
    assert "# >>> taskmux trust-clients >>>" in text


def test_apply_unchanged_when_block_identical(tmp_path: Path):
    rc = tmp_path / ".zshenv"
    shell_env.applyTrustClients(Path("/x/rootCA.pem"), "zsh", rcOverride=rc)
    mtime = os.path.getmtime(rc)
    contentBefore = rc.read_text()
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "zsh", rcOverride=rc)
    assert res["action"] == "unchanged"
    assert os.path.getmtime(rc) == mtime
    assert rc.read_text() == contentBefore


def test_apply_replaces_when_path_changes(tmp_path: Path):
    rc = tmp_path / ".zshenv"
    rc.write_text("# top\n")
    shell_env.applyTrustClients(Path("/old/rootCA.pem"), "zsh", rcOverride=rc)
    res = shell_env.applyTrustClients(Path("/new/rootCA.pem"), "zsh", rcOverride=rc)
    assert res["action"] == "replaced"
    text = rc.read_text()
    assert "/new/rootCA.pem" in text
    assert "/old/rootCA.pem" not in text
    assert text.startswith("# top\n")


def test_apply_collapses_duplicate_blocks(tmp_path: Path):
    rc = tmp_path / ".zshenv"
    block = shell_env.renderBlock(Path("/x/rootCA.pem"), "zsh")
    rc.write_text(f"# a\n{block}\n# middle\n{block}\n# tail\n")
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "zsh", rcOverride=rc)
    assert res["action"] == "replaced"
    text = rc.read_text()
    assert text.count("# >>> taskmux trust-clients >>>") == 1
    assert text.count("# <<< taskmux trust-clients <<<") == 1
    assert "# a\n" in text
    assert "# middle\n" in text
    assert "# tail\n" in text


def test_apply_preserves_symlink(tmp_path: Path):
    real = tmp_path / "real.zshenv"
    real.write_text("# real file\n")
    link = tmp_path / ".zshenv"
    link.symlink_to(real)
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "zsh", rcOverride=link)
    assert res["action"] == "wrote"
    assert link.is_symlink()
    assert link.resolve() == real.resolve()
    assert "NODE_EXTRA_CA_CERTS" in real.read_text()


def test_apply_preserves_crlf(tmp_path: Path):
    rc = tmp_path / ".bashrc"
    rc.write_bytes(b"export PATH=/foo\r\nalias x=y\r\n")
    shell_env.applyTrustClients(Path("/x/rootCA.pem"), "bash", rcOverride=rc)
    raw = rc.read_bytes()
    assert b"\r\n" in raw
    assert b"\n\n" not in raw.replace(b"\r\n", b"")


def test_apply_windows_returns_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shell_env.os, "name", "nt")
    res = shell_env.applyTrustClients(Path("/x/rootCA.pem"), "zsh", rcOverride=tmp_path / ".zshenv")
    assert res["ok"] is False
    assert "POSIX-only" in res["error"]
