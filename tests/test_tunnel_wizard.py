"""Tests for the tunnel wizard orchestrator + cascade resolver + config safety.

API calls are mocked at the `aiohttp.ClientSession.request` level so we exercise
the real ``_api`` parsing, error handling, and ordering — not the HTTP wire.
"""

from __future__ import annotations

import asyncio
import os
import tomllib
from pathlib import Path

import pytest

from taskmux import tunnel_wizard
from taskmux.errors import TaskmuxError
from taskmux.global_config import (
    CloudflareGlobalConfig,
    GlobalConfig,
    TunnelGlobalConfig,
    globalConfigModeOk,
    loadGlobalConfig,
    writeGlobalConfig,
)
from taskmux.models import (
    CloudflareTunnelProjectConfig,
    TaskConfig,
    TaskmuxConfig,
    TunnelKind,
)
from taskmux.tunnel_wizard import describeTunnelConfig, setTunnelConfig
from taskmux.tunnels import resolveCloudflareConfig


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Cascade resolver
# ---------------------------------------------------------------------------


class TestCascade:
    def test_global_only(self, monkeypatch):
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
        gcf = CloudflareGlobalConfig(account_id="g-acct", zone_id="g-zone", api_token="g-tok")
        pcf = CloudflareTunnelProjectConfig()
        eff = resolveCloudflareConfig(global_cf=gcf, project_cf=pcf, project_id="proj")
        assert eff.account_id == "g-acct"
        assert eff.zone_id == "g-zone"
        assert eff.api_token == "g-tok"
        assert eff.tunnel_name == "taskmux-proj"
        assert eff.sources["account_id"] == "global"
        assert eff.sources["zone_id"] == "global"
        assert eff.sources["api_token"] == "global"
        assert eff.sources["tunnel_name"] == "default"

    def test_project_overrides_zone_and_tunnel_name(self):
        gcf = CloudflareGlobalConfig(zone_id="g-zone", account_id="acct", api_token="tok")
        pcf = CloudflareTunnelProjectConfig(zone_id="p-zone", tunnel_name="custom-name")
        eff = resolveCloudflareConfig(global_cf=gcf, project_cf=pcf, project_id="proj")
        assert eff.zone_id == "p-zone"
        assert eff.sources["zone_id"] == "project"
        assert eff.tunnel_name == "custom-name"
        assert eff.sources["tunnel_name"] == "project"

    def test_env_token_when_no_embedded(self, monkeypatch):
        monkeypatch.setenv("MY_CF_TOKEN", "from-env")
        gcf = CloudflareGlobalConfig(api_token=None, api_token_env="MY_CF_TOKEN")
        pcf = CloudflareTunnelProjectConfig()
        eff = resolveCloudflareConfig(global_cf=gcf, project_cf=pcf, project_id="proj")
        assert eff.api_token == "from-env"
        assert eff.sources["api_token"] == "env"

    def test_no_token_anywhere(self, monkeypatch):
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
        gcf = CloudflareGlobalConfig()
        pcf = CloudflareTunnelProjectConfig()
        eff = resolveCloudflareConfig(global_cf=gcf, project_cf=pcf, project_id="proj")
        assert eff.api_token is None

    def test_override_token_wins_over_global(self):
        gcf = CloudflareGlobalConfig(api_token="global")
        pcf = CloudflareTunnelProjectConfig()
        eff = resolveCloudflareConfig(
            global_cf=gcf, project_cf=pcf, project_id="proj", api_token_override="override"
        )
        assert eff.api_token == "override"
        assert eff.sources["api_token"] == "override"


# ---------------------------------------------------------------------------
# Global config IO + safety rails
# ---------------------------------------------------------------------------


class TestConfigIO:
    def test_legacy_flat_keys_migrate(self, tmp_path: Path, monkeypatch):
        # Pre-cascade ~/.taskmux/config.toml shape — fold into nested block.
        path = tmp_path / "config.toml"
        path.write_text(
            'cloudflare_account_id = "old-acct"\ncloudflare_api_token_env = "OLD_ENV"\n'
        )
        monkeypatch.setattr("taskmux.global_config.globalConfigPath", lambda: path)
        cfg = loadGlobalConfig(path)
        assert cfg.tunnel.cloudflare.account_id == "old-acct"
        assert cfg.tunnel.cloudflare.api_token_env == "OLD_ENV"

    def test_write_chmods_0600_when_token_embedded(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        cfg = GlobalConfig(
            tunnel=TunnelGlobalConfig(
                cloudflare=CloudflareGlobalConfig(api_token="secret123secret")
            )
        )
        writeGlobalConfig(cfg, path)
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

    def test_write_no_chmod_when_no_token(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        cfg = GlobalConfig(
            tunnel=TunnelGlobalConfig(cloudflare=CloudflareGlobalConfig(account_id="acct"))
        )
        writeGlobalConfig(cfg, path)
        # Write should not have forced 0600 — token absent.
        # We don't assert a specific mode (umask varies); just that the file is readable.
        assert path.exists()

    def test_global_config_mode_ok_with_no_token(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "config.toml"
        path.write_text("api_port = 8765\n")
        os.chmod(path, 0o644)
        monkeypatch.setattr("taskmux.global_config.globalConfigPath", lambda: path)
        ok, mode = globalConfigModeOk(path)
        assert ok
        assert mode == 0o644

    def test_round_trip_preserves_nested_block(self, tmp_path: Path):
        path = tmp_path / "config.toml"
        cfg = GlobalConfig(
            tunnel=TunnelGlobalConfig(
                cloudflare=CloudflareGlobalConfig(
                    account_id="acct", zone_id="zone", api_token="tok"
                )
            )
        )
        writeGlobalConfig(cfg, path)
        loaded = loadGlobalConfig(path)
        assert loaded.tunnel.cloudflare.account_id == "acct"
        assert loaded.tunnel.cloudflare.zone_id == "zone"
        assert loaded.tunnel.cloudflare.api_token == "tok"

    def test_api_token_rejected_in_project_block(self):
        with pytest.raises(TaskmuxError):
            CloudflareTunnelProjectConfig(api_token="leaked")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Mocked Cloudflare API helpers — used by preflight/enable tests
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        import json as _json

        return _json.dumps(self._payload)

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class _FakeSession:
    """Records every request and serves scripted responses keyed by (METHOD, suffix)."""

    def __init__(self, scripts: dict):
        self.scripts = dict(scripts)
        self.calls: list[tuple[str, str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), payload in self.scripts.items():
            if m == method and url.endswith(suffix):
                if isinstance(payload, list):
                    if not payload:
                        raise AssertionError(f"no more scripted responses for {method} {suffix}")
                    next_payload = payload.pop(0)
                else:
                    next_payload = payload
                status = next_payload.get("__status", 200)
                body = {k: v for k, v in next_payload.items() if k != "__status"}
                return _FakeResponse(status, body)
        raise AssertionError(f"unscripted call: {method} {url}")


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    monkeypatch.setattr("taskmux.tunnel_wizard.aiohttp.ClientSession", lambda *_a, **_kw: session)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _make_project_cfg(public_hostname: str = "api.example.com") -> TaskmuxConfig:
    task = TaskConfig(
        command="bun dev",
        host="api",
        tunnel=TunnelKind.CLOUDFLARE,
        public_hostname=public_hostname,
    )
    return TaskmuxConfig(name="proj", tasks={"api": task})


def _make_global_cfg(token: str | None = "tok") -> GlobalConfig:
    return GlobalConfig(
        tunnel=TunnelGlobalConfig(
            cloudflare=CloudflareGlobalConfig(account_id="acct", zone_id=None, api_token=token)
        )
    )


class TestPreflight:
    def test_no_token_short_circuits(self, monkeypatch):
        monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
        report = _run(
            tunnel_wizard.preflight(
                project_id="proj",
                project_cfg=_make_project_cfg(),
                global_cfg=_make_global_cfg(token=None),
            )
        )
        assert not report.ok
        token_check = next(c for c in report.checks if c.name == "api_token")
        assert not token_check.ok

    def test_full_preflight_ok(self, monkeypatch):
        scripts = {
            ("GET", "/user/tokens/verify"): {
                "success": True,
                "result": {"status": "active"},
            },
            ("GET", "/zones?per_page=50"): {
                "success": True,
                "result": [{"id": "zone-id", "name": "example.com"}],
            },
        }
        # /zones request goes via params; aiohttp serializes to ?per_page=50 in URL.
        # We match on the suffix "/zones" — strip any query string.
        scripts2 = {}
        for (m, s), p in scripts.items():
            scripts2[(m, s.split("?")[0])] = p
        sess = _FakeSession(scripts2)
        # Multiple zone-list calls happen (preflight calls _list_zones twice via
        # _check_account_and_zone and again for collisions).
        # Make scripts return the same payload repeatedly.
        sess.scripts[("GET", "/zones")] = {
            "success": True,
            "result": [{"id": "zone-id", "name": "example.com"}],
        }
        _patch_session(monkeypatch, sess)
        # Skip DNS-collision call: hostname "api.example.com" — script needs
        # GET /zones/zone-id/dns_records.
        sess.scripts[("GET", "/zones/zone-id/dns_records")] = {
            "success": True,
            "result": [],
        }

        report = _run(
            tunnel_wizard.preflight(
                project_id="proj",
                project_cfg=_make_project_cfg(),
                global_cfg=_make_global_cfg(),
            )
        )
        # cloudflared may be missing on CI; tolerate that one miss but assert
        # the other expected checks pass.
        names = {c.name for c in report.checks if c.ok}
        assert "api_token" in names
        assert "account_id" in names
        assert "hostname:api.example.com" in names
        assert any(c.name == "dns:api.example.com" and c.ok for c in report.checks)

    def test_dns_collision_detected(self, monkeypatch):
        sess = _FakeSession(
            {
                ("GET", "/user/tokens/verify"): {
                    "success": True,
                    "result": {"status": "active"},
                },
                ("GET", "/zones"): {
                    "success": True,
                    "result": [{"id": "zone-id", "name": "example.com"}],
                },
                ("GET", "/zones/zone-id/dns_records"): {
                    "success": True,
                    "result": [
                        {"type": "A", "content": "1.2.3.4"},
                    ],
                },
            }
        )
        _patch_session(monkeypatch, sess)
        report = _run(
            tunnel_wizard.preflight(
                project_id="proj",
                project_cfg=_make_project_cfg(),
                global_cfg=_make_global_cfg(),
            )
        )
        col = next(c for c in report.checks if c.name == "dns:api.example.com")
        assert not col.ok
        assert "1.2.3.4" in col.detail

    def test_existing_taskmux_tunnel_cname_is_fine(self, monkeypatch):
        sess = _FakeSession(
            {
                ("GET", "/user/tokens/verify"): {
                    "success": True,
                    "result": {"status": "active"},
                },
                ("GET", "/zones"): {
                    "success": True,
                    "result": [{"id": "zone-id", "name": "example.com"}],
                },
                ("GET", "/zones/zone-id/dns_records"): {
                    "success": True,
                    "result": [
                        {"type": "CNAME", "content": "tunnel-uuid.cfargotunnel.com"},
                    ],
                },
            }
        )
        _patch_session(monkeypatch, sess)
        report = _run(
            tunnel_wizard.preflight(
                project_id="proj",
                project_cfg=_make_project_cfg(),
                global_cfg=_make_global_cfg(),
                tunnel_id="tunnel-uuid",
            )
        )
        col = next(c for c in report.checks if c.name == "dns:api.example.com")
        assert col.ok


# ---------------------------------------------------------------------------
# describe / setTunnelConfig (CLI/WS shared)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_taskmux_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "taskmux-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home.parent))
    monkeypatch.setattr("taskmux.paths.TASKMUX_DIR", home)
    monkeypatch.setattr("taskmux.paths.GLOBAL_CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr("taskmux.global_config.globalConfigPath", lambda: home / "config.toml")
    monkeypatch.setattr("taskmux.paths.globalConfigPath", lambda: home / "config.toml")
    monkeypatch.setattr("taskmux.paths.PROJECTS_DIR", home / "projects")
    return home


class TestDescribeAndSet:
    def test_describe_masks_token(self, isolated_taskmux_home: Path, tmp_path: Path):
        # Write a global config with a token.
        gcfg = GlobalConfig(
            tunnel=TunnelGlobalConfig(
                cloudflare=CloudflareGlobalConfig(
                    account_id="acct", zone_id="zone", api_token="cf-pat-AAAABBBBCCCC"
                )
            )
        )
        writeGlobalConfig(gcfg)
        # Write a minimal project config alongside.
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / "taskmux.toml").write_text('name = "myproj"\n')
        payload = describeTunnelConfig(config_path=project_dir / "taskmux.toml")
        token_value = payload["effective"]["api_token"]["value"]
        assert token_value is not None
        assert "cf-pat-AAAABBBBCCCC" not in token_value
        assert payload["effective"]["api_token"]["masked"] is True

    def test_describe_reveal_shows_full_token(self, isolated_taskmux_home: Path, tmp_path: Path):
        gcfg = GlobalConfig(
            tunnel=TunnelGlobalConfig(
                cloudflare=CloudflareGlobalConfig(
                    account_id="acct", zone_id="zone", api_token="cf-pat-FULL"
                )
            )
        )
        writeGlobalConfig(gcfg)
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / "taskmux.toml").write_text('name = "myproj"\n')
        # Reveal applies to the global block, not the effective masked entry.
        payload = describeTunnelConfig(config_path=project_dir / "taskmux.toml", reveal=True)
        assert payload["global"]["api_token"] == "cf-pat-FULL"

    def test_set_global_zone_id_via_flat_key(self, isolated_taskmux_home: Path):
        setTunnelConfig(scope="global", updates={"zone_id": "zzz"})
        cfg = loadGlobalConfig()
        assert cfg.tunnel.cloudflare.zone_id == "zzz"

    def test_set_global_with_dotted_path(self, isolated_taskmux_home: Path):
        setTunnelConfig(scope="global", updates={"tunnel.cloudflare.account_id": "abc"})
        cfg = loadGlobalConfig()
        assert cfg.tunnel.cloudflare.account_id == "abc"

    def test_set_project_rejects_api_token(self, isolated_taskmux_home: Path, tmp_path: Path):
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / "taskmux.toml").write_text('name = "myproj"\n')
        with pytest.raises(TaskmuxError):
            setTunnelConfig(
                scope="project",
                updates={"api_token": "leaked"},
                config_path=project_dir / "taskmux.toml",
            )

    def test_set_project_zone_id(self, isolated_taskmux_home: Path, tmp_path: Path):
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / "taskmux.toml").write_text('name = "myproj"\n')
        setTunnelConfig(
            scope="project",
            updates={"zone_id": "p-zone"},
            config_path=project_dir / "taskmux.toml",
        )
        loaded = tomllib.loads((project_dir / "taskmux.toml").read_text())
        assert loaded["tunnel"]["cloudflare"]["zone_id"] == "p-zone"


# ---------------------------------------------------------------------------
# disable
# ---------------------------------------------------------------------------


class TestDisable:
    def test_disable_strips_tunnel_fields(self, isolated_taskmux_home: Path, tmp_path: Path):
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / "taskmux.toml").write_text(
            'name = "myproj"\n\n'
            "[tunnel.cloudflare]\n"
            'zone_id = "z"\n\n'
            "[tasks.api]\n"
            'command = "bun dev"\n'
            'host = "api"\n'
            'tunnel = "cloudflare"\n'
            'public_hostname = "api.example.com"\n'
        )

        async def _run_disable():
            return await tunnel_wizard.disable(
                config_path=project_dir / "taskmux.toml", prune=False
            )

        out = _run(_run_disable())
        assert out["ok"]
        loaded = tomllib.loads((project_dir / "taskmux.toml").read_text())
        api = loaded["tasks"]["api"]
        assert "tunnel" not in api
        assert "public_hostname" not in api
        # Without prune, the [tunnel.cloudflare] block stays.
        assert loaded.get("tunnel", {}).get("cloudflare", {}).get("zone_id") == "z"

    def test_disable_prune_removes_tunnel_block(self, isolated_taskmux_home: Path, tmp_path: Path):
        project_dir = tmp_path / "myproj"
        project_dir.mkdir()
        (project_dir / "taskmux.toml").write_text(
            'name = "myproj"\n\n'
            "[tunnel.cloudflare]\n"
            'zone_id = "z"\n\n'
            "[tasks.api]\n"
            'command = "bun dev"\n'
            'host = "api"\n'
            'tunnel = "cloudflare"\n'
            'public_hostname = "api.example.com"\n'
        )

        async def _run_disable():
            return await tunnel_wizard.disable(config_path=project_dir / "taskmux.toml", prune=True)

        _run(_run_disable())
        loaded = tomllib.loads((project_dir / "taskmux.toml").read_text())
        assert "tunnel" not in loaded
