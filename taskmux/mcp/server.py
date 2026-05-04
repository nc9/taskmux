"""FastMCP server scaffold for taskmux.

`buildServer(dispatch)` returns a FastMCP instance pre-registered with
taskmux's tools and resources. The daemon owns construction and exposes the
resulting `streamable_http_app()` at `/mcp` on `api_port`.

`dispatch` is the daemon's `_handle_api_request` (or any awaitable matching
the same shape: `(payload: dict) -> dict`). Injecting it lets tests stub
arbitrary command handlers without spinning up the daemon, and keeps the
dependency edge one-way: MCP module never imports from `daemon`.

Tools and project-specific resources are pin-aware: when the connecting
URL carried `?session=foo`, the request's contextvar holds the pin and
`session` arguments are optional (default to the pin) / rejected when they
disagree (`pin_violation`). See `taskmux.mcp.scope`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..events import queryEvents
from .scope import PinViolation, currentPin, resolveSession

DispatchFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def buildServer(dispatch: DispatchFn, *, name: str = "taskmux") -> FastMCP:
    """Construct a FastMCP server with taskmux tools + resources registered.

    The streamable-HTTP route is rebound to `/` inside the returned app so the
    daemon can mount it at `/mcp` on its parent router without a `/mcp/mcp`
    double-prefix.
    """
    mcp = FastMCP(name, instructions=_INSTRUCTIONS, streamable_http_path="/")

    # ---- pin helpers (closed over `dispatch`) ----

    async def _statusForPin(pin: str | None) -> dict[str, Any]:
        """Return a `status_all`-shaped envelope, filtered to one session
        when pinned. Same outer shape regardless of pin so clients never
        have to branch on the connection mode.
        """
        snapshot = await dispatch({"command": "status_all"})
        if pin is None:
            return snapshot
        data = snapshot.get("data", snapshot)
        projects = [p for p in data.get("projects", []) if p.get("session") == pin]
        return {
            "command": "status_all",
            "data": {
                "projects": projects,
                "count": len(projects),
                "timestamp": data.get("timestamp"),
            },
        }

    # ---- tools ----

    @mcp.tool()
    async def taskmux_status() -> dict[str, Any]:
        """Snapshot of every project, session, and task.

        Same shape as `taskmux status --json`. When this MCP connection is
        pinned via `?session=` the snapshot is filtered to the pinned
        project; otherwise every loaded project appears.
        """
        return await _statusForPin(currentPin.get())

    @mcp.tool()
    async def taskmux_list_projects() -> dict[str, Any]:
        """List every project the daemon knows about (loaded or
        config_missing). Stays global even on pinned connections — pinned
        agents still need to know which sibling projects exist.
        """
        return await dispatch({"command": "list_projects"})

    @mcp.tool()
    async def taskmux_inspect(task: str, session: str | None = None) -> dict[str, Any]:
        """Detailed status for one task: pid, exit code, restart count,
        last health result, recent events. `session` is optional on a pinned
        connection (defaults to the pin) and required otherwise.
        """
        return await _dispatchPerTask(dispatch, "inspect", session, task)

    @mcp.tool()
    async def taskmux_logs(
        task: str,
        session: str | None = None,
        lines: int = 100,
        grep: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Tail of a task's log file. `grep` filters lines client-side;
        `since` accepts `5m`, `1h`, ISO8601, etc.
        """
        try:
            resolved = resolveSession(session)
        except PinViolation as e:
            return e.to_dict()
        if resolved is None:
            return {"error": "missing_session", "command": "logs"}
        params: dict[str, Any] = {"session": resolved, "task": task, "lines": lines}
        if grep is not None:
            params["grep"] = grep
        if since is not None:
            params["since"] = since
        return await dispatch({"command": "logs", "params": params})

    @mcp.tool()
    async def taskmux_start(task: str, session: str | None = None) -> dict[str, Any]:
        """Start a stopped task."""
        return await _dispatchPerTask(dispatch, "start", session, task)

    @mcp.tool()
    async def taskmux_stop(task: str, session: str | None = None) -> dict[str, Any]:
        """Manual stop. Suppresses auto-restart until the next manual
        start/restart.
        """
        return await _dispatchPerTask(dispatch, "stop", session, task)

    @mcp.tool()
    async def taskmux_restart(task: str, session: str | None = None) -> dict[str, Any]:
        """Restart a task. Clears the manually-stopped flag."""
        return await _dispatchPerTask(dispatch, "restart", session, task)

    @mcp.tool()
    async def taskmux_kill(task: str, session: str | None = None) -> dict[str, Any]:
        """SIGKILL a task immediately. Use only when graceful stop hangs."""
        return await _dispatchPerTask(dispatch, "kill", session, task)

    @mcp.tool()
    async def taskmux_health(task: str, session: str | None = None) -> dict[str, Any]:
        """Force a health check now (rather than waiting for the next sweep)."""
        return await _dispatchPerTask(dispatch, "health", session, task)

    @mcp.tool()
    async def taskmux_events(
        session: str | None = None,
        task: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Recent lifecycle events for a project. Filter by task and time."""
        try:
            resolved = resolveSession(session)
        except PinViolation as e:
            return e.to_dict()
        if resolved is None:
            return {"error": "missing_session", "command": "events"}
        params: dict[str, Any] = {"session": resolved, "limit": limit}
        if task is not None:
            params["task"] = task
        if since is not None:
            params["since"] = since
        return await dispatch({"command": "events", "params": params})

    # ---- resources ----

    @mcp.resource("taskmux://status", mime_type="application/json")
    async def statusResource() -> str:
        return json.dumps(await _statusForPin(currentPin.get()), default=str)

    @mcp.resource("taskmux://projects", mime_type="application/json")
    async def projectsResource() -> str:
        return json.dumps(await dispatch({"command": "list_projects"}), default=str)

    @mcp.resource("taskmux://events/recent", mime_type="application/json")
    async def eventsResource() -> str:
        """Last 100 lifecycle events. Reads `~/.taskmux/events.jsonl`
        directly so it works even when no project session is loaded;
        filtered to the pin when the connection is pinned.
        """
        pin = currentPin.get()
        events = queryEvents(session=pin, limit=100) if pin else queryEvents(limit=100)
        return json.dumps(events, default=str)

    @mcp.resource("taskmux://logs/{session}/{task}", mime_type="text/plain")
    async def logsResource(session: str, task: str) -> str:
        try:
            resolved = resolveSession(session)
        except PinViolation as e:
            return json.dumps(e.to_dict())
        # `resolved` is only None when unpinned + arg None, which can't
        # happen here (URI template supplies session).
        result = await dispatch(
            {
                "command": "logs",
                "params": {"session": resolved, "task": task, "lines": 200},
            }
        )
        return "\n".join(result.get("lines", []))

    return mcp


async def _dispatchPerTask(
    dispatch: DispatchFn, command: str, session: str | None, task: str
) -> dict[str, Any]:
    """Shared shape for the per-task tools.

    Resolves the session against the pin, returns a `pin_violation`
    payload on mismatch, or a `missing_session` error when the connection
    is unpinned and the caller didn't pass one.
    """
    try:
        resolved = resolveSession(session)
    except PinViolation as e:
        return e.to_dict()
    if resolved is None:
        return {"error": "missing_session", "command": command}
    return await dispatch({"command": command, "params": {"session": resolved, "task": task}})


_INSTRUCTIONS = """\
taskmux is a tmux-backed task supervisor. This MCP server exposes:

  * tools to inspect and control managed tasks
  * resources reflecting live session/task state
  * notifications when tasks crash, restart, or fail health checks

When a task fails or restarts you'll receive a `notifications/message` event.
Follow up with `taskmux_logs` to investigate before suggesting fixes.

## Connection scoping

This server may be pinned to a specific project via a `?session=foo` URL
parameter at connect time. When pinned:

  * `taskmux_status` and `taskmux://status` return that project only
  * `notifications/message` only fires for events from that project
  * Per-task tools (`taskmux_inspect`, `taskmux_logs`, `taskmux_start`,
    `taskmux_stop`, `taskmux_restart`, `taskmux_kill`, `taskmux_health`,
    `taskmux_events`) take an optional `session` arg — omit it and the
    pin is used; pass a non-matching value and you'll get
    `{"error": "pin_violation", ...}`
  * `taskmux_list_projects` and `taskmux://projects` stay global so a
    pinned agent can still discover sibling projects exist

When unpinned (admin / diagnostic mode), `session` is required on every
per-task tool and `taskmux_status` returns the full daemon view.
"""
