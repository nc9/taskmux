"""Tests for taskmux.mcp.server (FastMCP wrappers around daemon dispatch)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from taskmux.mcp.server import buildServer


def _fakeStatus() -> dict[str, Any]:
    return {
        "projects": [
            {"session": "demo", "state": "ok", "tasks": {"api": {"healthy": True}}},
        ],
        "count": 1,
        "timestamp": "2026-05-04T00:00:00",
    }


def _makeDispatch(
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]] | dict[str, Any]] | None = None,
):
    """Build an awaitable dispatch fake. Each handler value is either a
    static dict (returned as-is) or a callable that takes params and returns
    a dict.
    """
    handlers = handlers or {}

    async def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
        cmd = payload["command"]
        params = payload.get("params", {})
        h = handlers.get(cmd)
        if h is None:
            return {"error": "no_handler", "command": cmd}
        if callable(h):
            return h(params)
        return h

    return dispatch


# Late import keeps the module importable without typing aliases at top level
from collections.abc import Callable  # noqa: E402


def testServerRegistersExpectedTools() -> None:
    async def run() -> None:
        server = buildServer(_makeDispatch())
        tools = await server.list_tools()
        names = {t.name for t in tools}
        for expected in [
            "taskmux_status",
            "taskmux_list_projects",
            "taskmux_inspect",
            "taskmux_logs",
            "taskmux_start",
            "taskmux_stop",
            "taskmux_restart",
            "taskmux_kill",
            "taskmux_health",
            "taskmux_events",
        ]:
            assert expected in names, f"missing tool {expected}"

    asyncio.run(run())


def testServerRegistersExpectedResources() -> None:
    async def run() -> None:
        server = buildServer(_makeDispatch())
        resources = await server.list_resources()
        templates = await server.list_resource_templates()
        uris = {str(r.uri) for r in resources} | {str(t.uriTemplate) for t in templates}
        for expected in [
            "taskmux://status",
            "taskmux://projects",
            "taskmux://events/recent",
            "taskmux://logs/{session}/{task}",
        ]:
            assert expected in uris, f"missing resource {expected}"

    asyncio.run(run())


def testStatusToolDispatchesStatusAll() -> None:
    async def run() -> None:
        seen: list[dict] = []

        async def dispatch(payload: dict) -> dict:
            seen.append(payload)
            return _fakeStatus()

        server = buildServer(dispatch)
        _, structured = await server.call_tool("taskmux_status", {})
        assert seen == [{"command": "status_all"}]
        assert structured == _fakeStatus()

    asyncio.run(run())


def testInspectToolForwardsSessionAndTask() -> None:
    async def run() -> None:
        seen: list[dict] = []

        async def dispatch(payload: dict) -> dict:
            seen.append(payload)
            return {"ok": True, "task": "api", "pid": 12345}

        server = buildServer(dispatch)
        await server.call_tool("taskmux_inspect", {"session": "demo", "task": "api"})
        assert seen == [{"command": "inspect", "params": {"session": "demo", "task": "api"}}]

    asyncio.run(run())


def testLogsToolDropsNoneOptionalParams() -> None:
    """Optional grep/since must not appear in the dispatched payload when
    None — the daemon treats missing keys differently from explicit None.
    """

    async def run() -> None:
        seen: list[dict] = []

        async def dispatch(payload: dict) -> dict:
            seen.append(payload)
            return {"lines": []}

        server = buildServer(dispatch)
        await server.call_tool("taskmux_logs", {"session": "demo", "task": "api", "lines": 50})
        assert seen[0] == {
            "command": "logs",
            "params": {"session": "demo", "task": "api", "lines": 50},
        }
        assert "grep" not in seen[0]["params"]
        assert "since" not in seen[0]["params"]

    asyncio.run(run())


def testEventsToolThreadsAllParams() -> None:
    async def run() -> None:
        seen: list[dict] = []

        async def dispatch(payload: dict) -> dict:
            seen.append(payload)
            return {"events": []}

        server = buildServer(dispatch)
        await server.call_tool(
            "taskmux_events",
            {"session": "demo", "task": "api", "since": "5m", "limit": 10},
        )
        assert seen[0] == {
            "command": "events",
            "params": {"session": "demo", "task": "api", "since": "5m", "limit": 10},
        }

    asyncio.run(run())


def testStatusResourceReturnsJson() -> None:
    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            return _fakeStatus()

        server = buildServer(dispatch)
        contents = await server.read_resource("taskmux://status")
        body = next(iter(contents)).content
        assert json.loads(body) == _fakeStatus()

    asyncio.run(run())


def testLogsResourceReadsViaDispatch() -> None:
    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            assert payload["command"] == "logs"
            assert payload["params"] == {"session": "demo", "task": "api", "lines": 200}
            return {"lines": ["line one", "line two"]}

        server = buildServer(dispatch)
        contents = await server.read_resource("taskmux://logs/demo/api")
        body = next(iter(contents)).content
        assert body == "line one\nline two"

    asyncio.run(run())


def testStreamableHttpAppMountsAtRoot() -> None:
    """Confirm the streamable_http_path override took effect — without it the
    daemon's `Mount('/mcp', app)` would resolve to /mcp/mcp."""
    server = buildServer(_makeDispatch())
    app = server.streamable_http_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/" in paths
    assert "/mcp" not in paths


def testStatusToolFiltersToSingleProjectWhenPinned() -> None:
    """`taskmux_status` returns the same outer shape but filtered when
    pinned via the connection's contextvar."""
    from taskmux.mcp.scope import currentPin

    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            assert payload["command"] == "status_all"
            return {
                "command": "status_all",
                "data": {
                    "projects": [
                        {"session": "demo", "state": "ok", "tasks": {}},
                        {"session": "other", "state": "ok", "tasks": {}},
                    ],
                    "count": 2,
                    "timestamp": "2026-05-04T00:00:00",
                },
            }

        server = buildServer(dispatch)
        token = currentPin.set("demo")
        try:
            _, structured = await server.call_tool("taskmux_status", {})
        finally:
            currentPin.reset(token)
        assert structured["data"]["count"] == 1
        assert [p["session"] for p in structured["data"]["projects"]] == ["demo"]

    asyncio.run(run())


def testInspectToolDefaultsToPinWhenSessionOmitted() -> None:
    from taskmux.mcp.scope import currentPin

    async def run() -> None:
        seen: list[dict] = []

        async def dispatch(payload: dict) -> dict:
            seen.append(payload)
            return {"ok": True}

        server = buildServer(dispatch)
        token = currentPin.set("demo")
        try:
            await server.call_tool("taskmux_inspect", {"task": "api"})
        finally:
            currentPin.reset(token)
        assert seen == [{"command": "inspect", "params": {"session": "demo", "task": "api"}}]

    asyncio.run(run())


def testInspectToolMatchingPinPasses() -> None:
    """Caller can still pass `session` explicitly as long as it matches."""
    from taskmux.mcp.scope import currentPin

    async def run() -> None:
        seen: list[dict] = []

        async def dispatch(payload: dict) -> dict:
            seen.append(payload)
            return {"ok": True}

        server = buildServer(dispatch)
        token = currentPin.set("demo")
        try:
            await server.call_tool("taskmux_inspect", {"session": "demo", "task": "api"})
        finally:
            currentPin.reset(token)
        assert seen[0]["params"] == {"session": "demo", "task": "api"}

    asyncio.run(run())


def testInspectToolMismatchedPinReturnsViolation() -> None:
    from taskmux.mcp.scope import currentPin

    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            raise AssertionError("dispatch must not run on pin violation")

        server = buildServer(dispatch)
        token = currentPin.set("demo")
        try:
            _, structured = await server.call_tool(
                "taskmux_inspect", {"session": "domaingenius", "task": "api"}
            )
        finally:
            currentPin.reset(token)
        assert structured["error"] == "pin_violation"
        assert structured["pinned_session"] == "demo"
        assert structured["requested_session"] == "domaingenius"

    asyncio.run(run())


def testEventsToolMissingSessionWhenUnpinned() -> None:
    """Without a pin and without a session arg, return the missing_session
    error rather than dispatching with a None session."""

    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            raise AssertionError("dispatch must not run when session missing")

        server = buildServer(dispatch)
        _, structured = await server.call_tool("taskmux_events", {})
        assert structured["error"] == "missing_session"

    asyncio.run(run())


def testEventsResourceFiltersByPin() -> None:
    """`taskmux://events/recent` calls queryEvents with session=pin when pinned."""
    from unittest.mock import patch

    from taskmux.mcp.scope import currentPin

    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            return {}

        server = buildServer(dispatch)
        captured: list[dict] = []

        def fakeQuery(**kwargs):
            captured.append(kwargs)
            return [{"event": "task_started", "session": kwargs.get("session", "all")}]

        token = currentPin.set("demo")
        try:
            with patch("taskmux.mcp.server.queryEvents", fakeQuery):
                contents = await server.read_resource("taskmux://events/recent")
        finally:
            currentPin.reset(token)
        assert captured == [{"session": "demo", "limit": 100}]
        body = next(iter(contents)).content
        assert json.loads(body) == [{"event": "task_started", "session": "demo"}]

    asyncio.run(run())


def testLogsResourceRejectsMismatchedPin() -> None:
    from taskmux.mcp.scope import currentPin

    async def run() -> None:
        async def dispatch(payload: dict) -> dict:
            raise AssertionError("dispatch must not run on pin violation")

        server = buildServer(dispatch)
        token = currentPin.set("demo")
        try:
            contents = await server.read_resource("taskmux://logs/other/api")
        finally:
            currentPin.reset(token)
        body = json.loads(next(iter(contents)).content)
        assert body["error"] == "pin_violation"
        assert body["requested_session"] == "other"

    asyncio.run(run())


def testParentMountResolvesMcpPath() -> None:
    """Mirror the daemon's mount layout: WS at '/', MCP at '/mcp'.

    A POST to /mcp without `Accept: text/event-stream` is rejected by the
    SDK with 406 Not Acceptable — that's still proof the route resolved.
    A 404 here would mean the mount is wrong.
    """
    import httpx
    from starlette.applications import Starlette
    from starlette.routing import Mount

    async def run() -> None:
        server = buildServer(_makeDispatch())
        mcp_app = server.streamable_http_app()
        parent = Starlette(
            routes=[Mount("/mcp", app=mcp_app)],
            lifespan=mcp_app.router.lifespan_context,
        )
        transport = httpx.ASGITransport(app=parent)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert r.status_code != 404, f"mount missed; got {r.status_code} {r.text}"

    asyncio.run(run())
