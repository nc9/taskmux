"""Tests for taskmux.tunnels — TunnelBackend protocol + Cloudflare backend.

The Cloudflare backend is exercised against a stubbed `_api` method (it owns
the only path that touches the network). cloudflared spawning is also stubbed
since we don't want a real subprocess in unit tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from taskmux.errors import TaskmuxError
from taskmux.models import (
    CloudflareTunnelProjectConfig,
    TaskConfig,
    TaskmuxConfig,
    TunnelKind,
    TunnelProjectConfig,
)
from taskmux.tunnels import (
    CloudflareTunnelBackend,
    NoopTunnelBackend,
)


def _run(coro):
    return asyncio.run(coro)


def _make_backend(tmp_path: Path, *, tunnel_id: str | None = None, token: str | None = None):
    state_path = tmp_path / "tunnels" / "cloudflare" / "tname.json"
    if tunnel_id and token:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"tunnel_id": tunnel_id, "tunnel_name": "tname", "token": token})
        )
    return CloudflareTunnelBackend(
        account_id="acct",
        api_token="tok",
        zone_id="zone",
        tunnel_name="tname",
        proxy_port=443,
        state_path=state_path,
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_tunnel_requires_host(self):
        with pytest.raises(TaskmuxError):
            TaskConfig(
                command="bun dev",
                tunnel=TunnelKind.CLOUDFLARE,
                public_hostname="api.example.com",
            )

    def test_tunnel_requires_public_hostname_for_cloudflare(self):
        with pytest.raises(TaskmuxError):
            TaskConfig(
                command="bun dev",
                host="api",
                tunnel=TunnelKind.CLOUDFLARE,
            )

    def test_wildcard_host_cannot_be_tunneled(self):
        with pytest.raises(TaskmuxError):
            TaskConfig(
                command="bun dev",
                host="*",
                tunnel=TunnelKind.CLOUDFLARE,
                public_hostname="api.example.com",
            )

    def test_invalid_public_hostname(self):
        with pytest.raises(TaskmuxError):
            TaskConfig(command="bun dev", host="api", public_hostname="not_a_host")

    def test_public_hostname_is_lowercased_and_stripped(self):
        cfg = TaskConfig(command="bun dev", host="api", public_hostname=" API.example.com. ")
        assert cfg.public_hostname == "api.example.com"

    def test_project_requires_zone_id_when_tunneling(self):
        task = TaskConfig(
            command="bun dev",
            host="api",
            tunnel=TunnelKind.CLOUDFLARE,
            public_hostname="api.example.com",
        )
        with pytest.raises(TaskmuxError):
            TaskmuxConfig(name="proj", tasks={"api": task})

    def test_project_with_zone_id_validates(self):
        task = TaskConfig(
            command="bun dev",
            host="api",
            tunnel=TunnelKind.CLOUDFLARE,
            public_hostname="api.example.com",
        )
        cfg = TaskmuxConfig(
            name="proj",
            tasks={"api": task},
            tunnel=TunnelProjectConfig(cloudflare=CloudflareTunnelProjectConfig(zone_id="z123")),
        )
        assert cfg.tunnel.cloudflare.zone_id == "z123"


# ---------------------------------------------------------------------------
# Cloudflare backend internals
# ---------------------------------------------------------------------------


class TestIngressShape:
    def test_ingress_includes_catchall_404(self, tmp_path: Path):
        b = _make_backend(tmp_path)
        ingress = b._build_ingress([("api.example.com", "api.proj.localhost", 443)])
        assert ingress[-1] == {"service": "http_status:404"}

    def test_ingress_origin_request_uses_internal_fqdn(self, tmp_path: Path):
        b = _make_backend(tmp_path)
        ingress = b._build_ingress([("api.example.com", "api.proj.localhost", 443)])
        assert ingress[0]["hostname"] == "api.example.com"
        assert ingress[0]["service"] == "https://localhost:443"
        origin = ingress[0]["originRequest"]
        assert origin["httpHostHeader"] == "api.proj.localhost"
        assert origin["originServerName"] == "api.proj.localhost"
        assert origin["noTLSVerify"] is True

    def test_ingress_is_sorted_by_hostname_for_determinism(self, tmp_path: Path):
        b = _make_backend(tmp_path)
        ingress = b._build_ingress(
            [
                ("zeta.example.com", "z.proj.localhost", 443),
                ("alpha.example.com", "a.proj.localhost", 443),
            ]
        )
        names = [e["hostname"] for e in ingress if "hostname" in e]
        assert names == ["alpha.example.com", "zeta.example.com"]


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class _StubAPI:
    """Records every _api call and returns scripted results."""

    def __init__(self, results):
        self.calls: list[tuple[str, str, dict]] = []
        self._results = list(results)

    async def __call__(self, _session, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if not self._results:
            return None
        return self._results.pop(0)


class TestSyncFlow:
    def test_sync_creates_tunnel_when_state_missing(self, tmp_path: Path, monkeypatch):
        b = _make_backend(tmp_path)
        scripted = [
            [],  # GET cfd_tunnel?name=tname → no existing
            {"id": "tunnel-uuid"},  # POST cfd_tunnel → created
            "the-token",  # GET token
            None,  # PUT configurations
            None,  # POST routes/dns
        ]
        stub = _StubAPI(scripted)
        b._api = stub  # type: ignore[method-assign]

        async def noop_session(*_a, **_kw):
            class _S:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *a):
                    return None

            return _S()

        # Stub aiohttp.ClientSession to a context manager yielding None.
        import taskmux.tunnels as tun_mod

        class _Sess:
            def __init__(self, *_, **__): ...

            async def __aenter__(self):
                return None

            async def __aexit__(self, *_):
                return None

        monkeypatch.setattr(tun_mod.aiohttp, "ClientSession", _Sess)

        async def stub_ensure_cf(_token):
            b._cloudflared_started_with = _token

        b._ensure_cloudflared = stub_ensure_cf  # type: ignore[method-assign]

        _run(b.sync([("api.example.com", "api.proj.localhost", 443)]))

        methods = [c[0] for c in stub.calls]
        paths = [c[1] for c in stub.calls]
        assert "POST" in methods
        # POST .../cfd_tunnel creates the tunnel (no trailing path).
        assert any(m == "POST" and p.endswith("/cfd_tunnel") for m, p, _ in stub.calls)
        assert any("/configurations" in p for p in paths)
        assert any("/routes/dns" in p for p in paths)
        assert b._tunnel_id == "tunnel-uuid"
        assert b._tunnel_token == "the-token"
        assert b._last_sync_ok is True
        assert b._last_mapping_count == 1

    def test_sync_reuses_state_when_tunnel_alive(self, tmp_path: Path, monkeypatch):
        b = _make_backend(tmp_path, tunnel_id="cached-id", token="cached-token")
        scripted = [
            {"id": "cached-id"},  # GET /cfd_tunnel/{id} validates
            None,  # PUT configurations
            None,  # POST routes/dns
        ]
        stub = _StubAPI(scripted)
        b._api = stub  # type: ignore[method-assign]

        import taskmux.tunnels as tun_mod

        class _Sess:
            def __init__(self, *_, **__): ...

            async def __aenter__(self):
                return None

            async def __aexit__(self, *_):
                return None

        monkeypatch.setattr(tun_mod.aiohttp, "ClientSession", _Sess)

        async def stub_ensure_cf(_token):
            return None

        b._ensure_cloudflared = stub_ensure_cf  # type: ignore[method-assign]

        _run(b.sync([("api.example.com", "api.proj.localhost", 443)]))

        # No POST /cfd_tunnel for creation.
        assert not any((m == "POST" and p.endswith("/cfd_tunnel")) for m, p, _ in stub.calls)

    def test_clear_parks_ingress_at_404(self, tmp_path: Path, monkeypatch):
        b = _make_backend(tmp_path, tunnel_id="t", token="tok")
        recorded: list[dict] = []

        async def fake_put_ingress(ingress: list[dict]) -> None:
            recorded.append(ingress)

        b._put_ingress = fake_put_ingress  # type: ignore[method-assign]

        async def fake_stop():
            b._stopped = True

        b._stop_cloudflared = fake_stop  # type: ignore[method-assign]

        _run(b.clear())

        assert recorded == [[{"service": "http_status:404"}]]
        assert getattr(b, "_stopped", False) is True
        assert b._last_sync_ok is False

    def test_state_persists_across_instances(self, tmp_path: Path):
        first = _make_backend(tmp_path)
        first._tunnel_id = "abc"
        first._tunnel_token = "tok"
        first._save_state()

        second = _make_backend(tmp_path)
        assert second._tunnel_id == "abc"
        assert second._tunnel_token == "tok"


class TestStatus:
    def test_status_reports_no_proc_when_idle(self, tmp_path: Path):
        b = _make_backend(tmp_path)
        s = b.status()
        assert s["backend"] == "cloudflare"
        assert s["cloudflared_running"] is False
        assert s["last_sync_ok"] is False
        assert s["mappings"] == 0


class TestNoopBackend:
    def test_noop_records_count(self):
        n = NoopTunnelBackend()
        _run(n.sync([("a.example.com", "a.proj.localhost", 443)]))
        s = n.status()
        assert s["backend"] == "noop"
        assert s["mappings"] == 1
        _run(n.clear())
        assert n.status()["mappings"] == 0

    def test_noop_public_url(self):
        n = NoopTunnelBackend()
        assert n.public_url("api.example.com") == "https://api.example.com/"


# ---------------------------------------------------------------------------
# Daemon-side mapping collection (validates `_collect_tunnel_mappings`)
# ---------------------------------------------------------------------------


class TestDaemonCollectMappings:
    def test_emits_mapping_for_running_tunneled_task(self, tmp_path: Path):
        from taskmux.daemon import TaskmuxDaemon

        # Build a config with one tunneled task.
        task = TaskConfig(
            command="bun dev",
            host="api",
            tunnel=TunnelKind.CLOUDFLARE,
            public_hostname="api.example.com",
        )
        cfg = TaskmuxConfig(
            name="proj",
            tasks={"api": task},
            tunnel=TunnelProjectConfig(cloudflare=CloudflareTunnelProjectConfig(zone_id="z123")),
        )

        d = TaskmuxDaemon.__new__(TaskmuxDaemon)
        d.configs = {"proj": cfg}

        class _FakeSup:
            def session_exists(self):
                return True

            def list_windows(self):
                return ["api"]

        d.projects = {"proj": _FakeSup()}

        from taskmux.global_config import GlobalConfig

        d.global_config = GlobalConfig()

        out = d._collect_tunnel_mappings("proj")
        assert "cloudflare" in out
        public, internal, port = out["cloudflare"][0]
        assert public == "api.example.com"
        assert internal == "api.proj.localhost"
        assert port == 443

    def test_skips_tunneled_task_that_is_not_running(self, tmp_path: Path):
        from taskmux.daemon import TaskmuxDaemon

        task = TaskConfig(
            command="bun dev",
            host="api",
            tunnel=TunnelKind.CLOUDFLARE,
            public_hostname="api.example.com",
        )
        cfg = TaskmuxConfig(
            name="proj",
            tasks={"api": task},
            tunnel=TunnelProjectConfig(cloudflare=CloudflareTunnelProjectConfig(zone_id="z123")),
        )

        d = TaskmuxDaemon.__new__(TaskmuxDaemon)
        d.configs = {"proj": cfg}

        class _FakeSup:
            def session_exists(self):
                return False

            def list_windows(self):
                return []

        d.projects = {"proj": _FakeSup()}

        from taskmux.global_config import GlobalConfig

        d.global_config = GlobalConfig()

        assert d._collect_tunnel_mappings("proj") == {}

    def test_apex_host_composes_to_project_fqdn(self, tmp_path: Path):
        from taskmux.daemon import TaskmuxDaemon

        task = TaskConfig(
            command="bun dev",
            host="@",
            tunnel=TunnelKind.CLOUDFLARE,
            public_hostname="example.com",
        )
        cfg = TaskmuxConfig(
            name="proj",
            tasks={"web": task},
            tunnel=TunnelProjectConfig(cloudflare=CloudflareTunnelProjectConfig(zone_id="z123")),
        )

        d = TaskmuxDaemon.__new__(TaskmuxDaemon)
        d.configs = {"proj": cfg}

        class _FakeSup:
            def session_exists(self):
                return True

            def list_windows(self):
                return ["web"]

        d.projects = {"proj": _FakeSup()}

        from taskmux.global_config import GlobalConfig

        d.global_config = GlobalConfig()

        _public, internal, _port = d._collect_tunnel_mappings("proj")["cloudflare"][0]
        assert internal == "proj.localhost"
