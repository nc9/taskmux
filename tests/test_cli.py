"""Tests for CLI commands — mock ipc_client to avoid daemon contact."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from taskmux.cli import app
from taskmux.config import ProjectIdentity
from taskmux.models import TaskmuxConfig

runner = CliRunner()


def _identity(project: str = "demo", config: TaskmuxConfig | None = None) -> ProjectIdentity:
    cfg = config if config is not None else TaskmuxConfig(name=project)
    return ProjectIdentity(
        config=cfg,
        config_path=Path(f"/tmp/{project}/taskmux.toml"),
        project=project,
        worktree_id=None,
        project_id=project,
        branch=None,
        worktree_path=None,
        primary_worktree_path=None,
    )


def _ok_result(action: str, task: str | None = None, session: str | None = None) -> dict:
    out: dict = {"ok": True, "action": action}
    if task is not None:
        out["task"] = task
    if session is not None:
        out["session"] = session
    return out


def _patch_ipc(call_response):
    """Patch ipc_client.call to return `call_response` (callable or constant)."""
    if callable(call_response):
        return patch("taskmux.cli.ipc_client.call", side_effect=call_response)
    return patch("taskmux.cli.ipc_client.call", return_value=call_response)


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "taskmux" in result.output.lower()


class TestJsonFlagHoist:
    """`--json` must work at every position, not just before the subcommand."""

    def test_hoists_post_subcommand(self):
        from taskmux.cli import _hoist_global_flags

        # After a subcommand:
        assert _hoist_global_flags(["daemon", "status", "--json"]) == [
            "--json",
            "daemon",
            "status",
        ]
        # In the middle:
        assert _hoist_global_flags(["daemon", "--json", "status"]) == [
            "--json",
            "daemon",
            "status",
        ]
        # Already in the right place — no-op:
        assert _hoist_global_flags(["--json", "daemon", "status"]) == [
            "--json",
            "daemon",
            "status",
        ]
        # No --json at all — no-op:
        assert _hoist_global_flags(["daemon", "status"]) == ["daemon", "status"]
        # --version is hoisted too:
        assert _hoist_global_flags(["daemon", "status", "-V"]) == [
            "-V",
            "daemon",
            "status",
        ]

    def test_does_not_steal_option_values(self):
        """`--grep --json` searches for literal `--json` — must not be hoisted."""
        from taskmux.cli import _hoist_global_flags

        assert _hoist_global_flags(["logs", "server", "--grep", "--json"]) == [
            "logs",
            "server",
            "--grep",
            "--json",
        ]
        assert _hoist_global_flags(["logs", "server", "-g", "--json"]) == [
            "logs",
            "server",
            "-g",
            "--json",
        ]
        # Boolean flags before --json still allow hoist:
        assert _hoist_global_flags(["logs", "--follow", "--json"]) == [
            "--json",
            "logs",
            "--follow",
        ]

    def test_respects_end_of_options_marker(self):
        """Tokens after `--` are positional data — never hoist."""
        from taskmux.cli import _hoist_global_flags

        assert _hoist_global_flags(["start", "--", "--json"]) == [
            "start",
            "--",
            "--json",
        ]
        # `--json` before `--` still hoists:
        assert _hoist_global_flags(["--json", "start", "--", "--json"]) == [
            "--json",
            "start",
            "--",
            "--json",
        ]


class TestInitCommand:
    @patch("taskmux.cli.initProject")
    def test_init_defaults(self, mock_init):
        result = runner.invoke(app, ["init", "--defaults"])
        assert result.exit_code == 0
        mock_init.assert_called_once_with(defaults=True)

    @patch("taskmux.cli.initProject")
    def test_init_interactive(self, mock_init):
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        mock_init.assert_called_once_with(defaults=False)


def _ipc_dispatch(command, params=None, **_):
    """Default fake ipc_client.call dispatch — returns ok results."""
    params = params or {}
    if command == "sync_registry":
        return {"ok": True}
    if command == "ping":
        return {"ok": True}
    if command in ("start", "stop", "restart", "kill"):
        return {"result": _ok_result(command + "ed", task=params.get("task"))}
    if command in ("start_all", "stop_all", "restart_all"):
        return {
            "result": _ok_result(
                {
                    "start_all": "started",
                    "stop_all": "stopped",
                    "restart_all": "restarted",
                }[command],
                session=params.get("session"),
            )
        }
    if command == "list_tasks":
        return {
            "data": {
                "session": params.get("session"),
                "running": False,
                "active_tasks": 0,
                "tasks": [],
            }
        }
    if command == "logs":
        if "task" in params:
            return {"lines": ["a", "b"]}
        return {"tasks": {"server": ["a"], "watcher": ["b"]}}
    if command == "inspect":
        return {"result": {"name": params.get("task"), "running": False}}
    if command == "health":
        return {"result": {"ok": False, "task": params.get("task"), "method": "proc"}}
    return {"result": {"ok": True}}


class TestStartCommand:
    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_start_all(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["start"])
        assert result.exit_code == 0
        commands = [c.args[0] for c in m.call_args_list]
        assert "start_all" in commands

    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_start_task(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["start", "server"])
        assert result.exit_code == 0
        called = [(c.args[0], c.kwargs.get("params", {}).get("task")) for c in m.call_args_list]
        assert ("start", "server") in called

    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_start_multiple(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["start", "api", "web"])
        assert result.exit_code == 0
        called = [(c.args[0], c.kwargs.get("params", {}).get("task")) for c in m.call_args_list]
        assert ("start", "api") in called
        assert ("start", "web") in called


class TestStopCommand:
    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_stop_all(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0
        commands = [c.args[0] for c in m.call_args_list]
        assert "stop_all" in commands

    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_stop_task(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["stop", "server"])
        assert result.exit_code == 0
        called = [(c.args[0], c.kwargs.get("params", {}).get("task")) for c in m.call_args_list]
        assert ("stop", "server") in called


class TestRestartCommand:
    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_restart_all(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["restart"])
        assert result.exit_code == 0
        commands = [c.args[0] for c in m.call_args_list]
        assert "restart_all" in commands

    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_restart_task(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["restart", "server"])
        assert result.exit_code == 0
        called = [(c.args[0], c.kwargs.get("params", {}).get("task")) for c in m.call_args_list]
        assert ("restart", "server") in called


class TestInspectCommand:
    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_inspect_calls_ipc(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["inspect", "server"])
        assert result.exit_code == 0
        called = [(c.args[0], c.kwargs.get("params", {}).get("task")) for c in m.call_args_list]
        assert ("inspect", "server") in called


class TestLogsCommand:
    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_logs_with_grep(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch) as m:
            result = runner.invoke(app, ["logs", "server", "--grep", "error"])
        assert result.exit_code == 0
        # logs RPC was called with the grep param
        params_calls = [c.kwargs.get("params", {}) for c in m.call_args_list if c.args[0] == "logs"]
        assert any(p.get("grep") == "error" for p in params_calls)

    @patch("taskmux.cli.registerProject")
    @patch("taskmux.cli.loadProjectIdentity")
    def test_logs_all(self, mock_load, _mock_reg, sample_toml: Path, monkeypatch):
        mock_load.return_value = _identity("demo")
        monkeypatch.chdir(sample_toml.parent)
        with _patch_ipc(_ipc_dispatch):
            result = runner.invoke(app, ["logs"])
        assert result.exit_code == 0


class TestAddCommand:
    def test_add_creates_task(self, sample_toml: Path):
        with patch("taskmux.cli.addTask") as mock_add:
            result = runner.invoke(app, ["add", "web", "npm start"])
            assert result.exit_code == 0
            mock_add.assert_called_once_with(
                None, "web", "npm start", cwd=None, host=None, health_check=None, depends_on=None
            )

    def test_add_with_options(self, sample_toml: Path):
        with patch("taskmux.cli.addTask") as mock_add:
            result = runner.invoke(
                app,
                [
                    "add",
                    "api",
                    "cargo run",
                    "--cwd",
                    "apps/api",
                    "--health-check",
                    "curl -sf localhost:4000/health",
                    "--depends-on",
                    "db",
                ],
            )
            assert result.exit_code == 0
            mock_add.assert_called_once_with(
                None,
                "api",
                "cargo run",
                cwd="apps/api",
                host=None,
                health_check="curl -sf localhost:4000/health",
                depends_on=["db"],
            )


class TestUrlCommand:
    def test_url_with_host(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.api]\ncommand = "x"\nhost = "api"\n')
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["url", "api"])
        assert result.exit_code == 0
        assert "https://api.demo.localhost" in result.output

    def test_url_without_host_fails(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.api]\ncommand = "x"\n')
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["url", "api"])
        assert result.exit_code == 1

    def test_url_unknown_task(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n')
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["url", "ghost"])
        assert result.exit_code == 1


class TestOpenCommand:
    def test_open_with_host(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.api]\ncommand = "x"\nhost = "api"\n')
        monkeypatch.chdir(tmp_path)
        with patch("webbrowser.open", return_value=True) as mock_open:
            result = runner.invoke(app, ["open", "api"])
        assert result.exit_code == 0
        mock_open.assert_called_once_with("https://api.demo.localhost")

    def test_open_json_mode(self, tmp_path: Path, monkeypatch):
        import json as _json

        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.api]\ncommand = "x"\nhost = "api"\n')
        monkeypatch.chdir(tmp_path)
        with patch("webbrowser.open", return_value=True):
            result = runner.invoke(app, ["--json", "open", "api"])
        assert result.exit_code == 0
        payload = _json.loads(result.output)
        assert payload == {"ok": True, "task": "api", "url": "https://api.demo.localhost"}

    def test_open_without_host_fails(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.api]\ncommand = "x"\n')
        monkeypatch.chdir(tmp_path)
        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(app, ["open", "api"])
        assert result.exit_code == 1
        mock_open.assert_not_called()

    def test_open_unknown_task(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n')
        monkeypatch.chdir(tmp_path)
        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(app, ["open", "ghost"])
        assert result.exit_code == 1
        mock_open.assert_not_called()

    def test_open_browser_failure_exits_nonzero(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.api]\ncommand = "x"\nhost = "api"\n')
        monkeypatch.chdir(tmp_path)
        with patch("webbrowser.open", return_value=False):
            result = runner.invoke(app, ["--json", "open", "api"])
        assert result.exit_code == 1
        assert '"ok": false' in result.output

    def test_open_wildcard_host_fails(self, tmp_path: Path, monkeypatch):
        cfg = tmp_path / "taskmux.toml"
        cfg.write_text('name = "demo"\n[tasks.catch]\ncommand = "x"\nhost = "*"\n')
        monkeypatch.chdir(tmp_path)
        with patch("webbrowser.open") as mock_open:
            result = runner.invoke(app, ["open", "catch"])
        assert result.exit_code == 1
        mock_open.assert_not_called()


class TestRemoveCommand:
    @patch("taskmux.cli.loadProjectIdentity")
    @patch("taskmux.cli.ipc_client.is_daemon_running", return_value=False)
    def test_remove_calls_removeTask(self, _mock_running, mock_load, sample_toml: Path):
        mock_load.return_value = _identity()
        with patch("taskmux.cli.removeTask", return_value=(TaskmuxConfig(), True)) as mock_rm:
            result = runner.invoke(app, ["remove", "server"])
            assert result.exit_code == 0
            mock_rm.assert_called_once_with(None, "server")


class TestCaTrustClients:
    @pytest.fixture
    def fake_bundle(self, tmp_path: Path, monkeypatch):
        """Stub mkcert root + system CA so trust-clients produces a real
        combined bundle under tmp_path."""
        from taskmux import ca
        from taskmux import paths as paths_mod

        monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path / ".taskmux")
        mkcert_pem = tmp_path / "rootCA.pem"
        mkcert_pem.write_text("MKCERT-FAKE\n")
        sys_pem = tmp_path / "system.pem"
        sys_pem.write_text("SYSROOT-FAKE\n")
        monkeypatch.setattr(ca, "systemCaBundle", lambda exclude=None: sys_pem)
        return mkcert_pem, ca.combinedBundlePath()

    def test_print_emits_export_lines(self, fake_bundle, monkeypatch):
        mkcert_pem, bundle = fake_bundle
        monkeypatch.setenv("SHELL", "/bin/zsh")
        with patch("taskmux.ca.caRootPath", return_value=mkcert_pem):
            result = runner.invoke(app, ["ca", "trust-clients", "--print", "--shell", "zsh"])
        assert result.exit_code == 0
        assert f"export NODE_EXTRA_CA_CERTS={bundle}" in result.output
        assert f"export REQUESTS_CA_BUNDLE={bundle}" in result.output
        assert f"export SSL_CERT_FILE={bundle}" in result.output
        body = bundle.read_text()
        assert "SYSROOT-FAKE" in body
        assert "MKCERT-FAKE" in body

    def test_print_fish_uses_set_gx(self, fake_bundle):
        mkcert_pem, bundle = fake_bundle
        with patch("taskmux.ca.caRootPath", return_value=mkcert_pem):
            result = runner.invoke(app, ["ca", "trust-clients", "--print", "--shell", "fish"])
        assert result.exit_code == 0
        assert f"set -gx NODE_EXTRA_CA_CERTS '{bundle}'" in result.output

    def test_print_json_mode_emits_only_json(self, fake_bundle):
        """--json --print must produce valid JSON, not raw exports + JSON."""
        import json

        mkcert_pem, bundle = fake_bundle
        with patch("taskmux.ca.caRootPath", return_value=mkcert_pem):
            result = runner.invoke(
                app, ["--json", "ca", "trust-clients", "--print", "--shell", "zsh"]
            )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["action"] == "printed"
        assert parsed["caPath"] == str(bundle)
        assert parsed["mkcertCaPath"] == str(mkcert_pem)
        assert "export NODE_EXTRA_CA_CERTS" in parsed["exports"]

    def test_writes_block_to_rc(self, fake_bundle, monkeypatch, tmp_path: Path):
        mkcert_pem, bundle = fake_bundle
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("taskmux.ca.caRootPath", return_value=mkcert_pem):
            result = runner.invoke(app, ["ca", "trust-clients", "--shell", "zsh"])
        assert result.exit_code == 0
        rc = tmp_path / ".zshenv"
        assert rc.exists()
        text = rc.read_text()
        assert "# >>> taskmux trust-clients >>>" in text
        assert f"export NODE_EXTRA_CA_CERTS={bundle}" in text
        assert str(mkcert_pem) not in text

    def test_missing_rootca_fails(self, tmp_path: Path):
        from taskmux.errors import ErrorCode, TaskmuxError

        def boom():
            raise TaskmuxError(ErrorCode.INTERNAL, detail="rootCA.pem not found")

        with patch("taskmux.ca.caRootPath", side_effect=boom):
            result = runner.invoke(app, ["ca", "trust-clients", "--shell", "zsh"])
        assert result.exit_code == 1

    def test_missing_system_ca_fails(self, tmp_path: Path, monkeypatch):
        from taskmux import ca
        from taskmux import paths as paths_mod

        monkeypatch.setattr(paths_mod, "TASKMUX_DIR", tmp_path / ".taskmux")
        mkcert_pem = tmp_path / "rootCA.pem"
        mkcert_pem.write_text("MK\n")
        monkeypatch.setattr(ca, "systemCaBundle", lambda exclude=None: None)
        with patch("taskmux.ca.caRootPath", return_value=mkcert_pem):
            result = runner.invoke(app, ["ca", "trust-clients", "--shell", "zsh"])
        assert result.exit_code == 1
        assert "system CA bundle not found" in result.output


# ---------------------------------------------------------------------------
# `taskmux mcp install` strict-mode rules
# ---------------------------------------------------------------------------


class TestMcpInstall:
    def test_outside_project_no_flags_fails(self, tmp_path: Path, monkeypatch):
        """No taskmux.toml in cwd or any ancestor → hard error with hint."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        result = runner.invoke(app, ["mcp", "install", "claude"])
        assert result.exit_code == 1
        assert "taskmux.toml not found" in result.output
        assert "--unscoped" in result.output

    def test_inside_project_writes_session_pinned_url(self, tmp_path: Path, monkeypatch):
        """`taskmux.toml` in cwd → install writes ?session=<name>."""
        import json as _json

        proj = tmp_path / "myproj"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "myproj"\n')
        monkeypatch.chdir(proj)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")

        result = runner.invoke(app, ["mcp", "install", "claude"])
        assert result.exit_code == 0, result.output
        target = tmp_path / "fake-home" / ".claude" / "settings.json"
        body = _json.loads(target.read_text())
        assert body["mcpServers"]["taskmux"]["url"].endswith("?session=myproj")

    def test_claude_project_from_subdir_writes_at_project_root(self, tmp_path: Path, monkeypatch):
        """Regression: `taskmux mcp install claude-project` from any
        descendant of a taskmux project writes `.mcp.json` at the
        project root, not the process cwd. Claude Code only loads
        `.mcp.json` from the repo root, so without this anchoring the
        install silently fails to take effect."""
        import json as _json

        proj = tmp_path / "myproj"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "myproj"\n')
        nested = proj / "src" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")

        result = runner.invoke(app, ["mcp", "install", "claude-project"])
        assert result.exit_code == 0, result.output

        body = _json.loads((proj / ".mcp.json").read_text())
        assert body["mcpServers"]["taskmux"]["url"].endswith("?session=myproj")
        assert not (nested / ".mcp.json").exists()

    def test_unscoped_flag_warns_and_writes_bare_url(self, tmp_path: Path, monkeypatch):
        import json as _json

        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")

        result = runner.invoke(app, ["mcp", "install", "claude", "--unscoped"])
        assert result.exit_code == 0, result.output
        assert "UNSCOPED" in result.output
        body = _json.loads((tmp_path / "fake-home" / ".claude" / "settings.json").read_text())
        assert "?session=" not in body["mcpServers"]["taskmux"]["url"]

    def test_session_flag_overrides_cwd_detection(self, tmp_path: Path, monkeypatch):
        """`--session foo` works from anywhere, no warning."""
        import json as _json

        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")

        result = runner.invoke(app, ["mcp", "install", "claude", "--session", "explicit"])
        assert result.exit_code == 0, result.output
        assert "UNSCOPED" not in result.output
        body = _json.loads((tmp_path / "fake-home" / ".claude" / "settings.json").read_text())
        assert body["mcpServers"]["taskmux"]["url"].endswith("?session=explicit")

    def test_unknown_client_rejected(self, tmp_path: Path, monkeypatch):
        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "p"\n')
        monkeypatch.chdir(proj)
        result = runner.invoke(app, ["mcp", "install", "notreal"])
        assert result.exit_code == 1
        assert "unknown client" in result.output

    def test_bare_install_non_tty_falls_back_to_all(self, tmp_path: Path, monkeypatch):
        """Script-friendly: no client arg + non-TTY (CliRunner) → install all,
        no prompt. Preserves the historical default for piped invocations."""
        import json as _json

        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "p"\n')
        monkeypatch.chdir(proj)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")

        result = runner.invoke(app, ["mcp", "install"])
        assert result.exit_code == 0, result.output

        # Each user-global / project target got written
        body = _json.loads((tmp_path / "fake-home" / ".claude" / "settings.json").read_text())
        assert body["mcpServers"]["taskmux"]["url"].endswith("?session=p")
        assert (proj / ".mcp.json").exists()
        assert (proj / ".codex" / "config.toml").exists()

    def test_bare_install_interactive_multi_select(self, tmp_path: Path, monkeypatch):
        """TTY path: questionary checkbox stubbed to return a subset →
        only those clients get installed."""
        from taskmux import cli as _cli

        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "p"\n')
        monkeypatch.chdir(proj)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        monkeypatch.setattr(_cli, "_stdinIsTty", lambda: True)
        monkeypatch.setattr(
            _cli,
            "_interactiveSelectClients",
            lambda cwd=None: ["claude-project", "codex-project"],
        )

        result = runner.invoke(app, ["mcp", "install"])
        assert result.exit_code == 0, result.output

        # claude-project + codex-project written
        assert (proj / ".mcp.json").exists()
        assert (proj / ".codex" / "config.toml").exists()
        # Other targets NOT written
        assert not (tmp_path / "fake-home" / ".claude" / "settings.json").exists()
        assert not (tmp_path / "fake-home" / ".cursor" / "mcp.json").exists()

    def test_bare_install_interactive_all_default(self, tmp_path: Path, monkeypatch):
        """Confirming the prompt with everything checked → install all."""
        from taskmux import cli as _cli
        from taskmux.mcp.install import ALL_CLIENTS

        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "p"\n')
        monkeypatch.chdir(proj)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        monkeypatch.setattr(_cli, "_stdinIsTty", lambda: True)
        monkeypatch.setattr(_cli, "_interactiveSelectClients", lambda cwd=None: list(ALL_CLIENTS))

        result = runner.invoke(app, ["mcp", "install"])
        assert result.exit_code == 0, result.output

        # User-global + project targets all written
        assert (tmp_path / "fake-home" / ".claude" / "settings.json").exists()
        assert (proj / ".mcp.json").exists()
        assert (proj / ".codex" / "config.toml").exists()

    def test_bare_install_interactive_cancelled(self, tmp_path: Path, monkeypatch):
        """Empty selection (Ctrl-C / no boxes ticked) → exit 1, no writes."""
        from taskmux import cli as _cli

        proj = tmp_path / "p"
        proj.mkdir()
        (proj / "taskmux.toml").write_text('name = "p"\n')
        monkeypatch.chdir(proj)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake-home")
        monkeypatch.setattr(_cli, "_stdinIsTty", lambda: True)
        monkeypatch.setattr(_cli, "_interactiveSelectClients", lambda cwd=None: [])

        result = runner.invoke(app, ["mcp", "install"])
        assert result.exit_code == 1
        assert not (proj / ".mcp.json").exists()

    def test_detect_installed_clients_project_only(self, tmp_path: Path, monkeypatch):
        """`_detectInstalledClients` only returns project-scoped clients.

        User-global agents never auto-check — a host-wide pin would expose
        every project on the machine, defeating per-project scoping.
        """
        from taskmux import cli as _cli

        proj = tmp_path / "p"
        proj.mkdir()
        (proj / ".cursor").mkdir()  # → cursor-project
        (proj / ".mcp.json").write_text("{}")  # → claude-project
        # User-global ~/.codex exists but MUST NOT be returned as detected.
        home = tmp_path / "fake-home"
        (home / ".codex").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)

        detected = _cli._detectInstalledClients(proj)
        assert detected == {"claude-project", "cursor-project"}
