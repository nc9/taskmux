"""Tests for CLI commands — mock ipc_client to avoid daemon contact."""

from pathlib import Path
from unittest.mock import patch

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


class TestRemoveCommand:
    @patch("taskmux.cli.loadProjectIdentity")
    @patch("taskmux.cli.ipc_client.is_daemon_running", return_value=False)
    def test_remove_calls_removeTask(self, _mock_running, mock_load, sample_toml: Path):
        mock_load.return_value = _identity()
        with patch("taskmux.cli.removeTask", return_value=(TaskmuxConfig(), True)) as mock_rm:
            result = runner.invoke(app, ["remove", "server"])
            assert result.exit_code == 0
            mock_rm.assert_called_once_with(None, "server")
