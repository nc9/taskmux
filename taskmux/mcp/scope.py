"""Per-connection session pinning for the daemon-hosted MCP server.

The daemon serves one MCP endpoint for every project on the host. Each
project's `.mcp.json` encodes which session it speaks for via a URL query
param (`http://localhost:{api_port}/mcp/?session=foo`). The
`PinExtractionMiddleware` reads that param on every request and stashes
the pin in a per-request ContextVar so:

  * pin-aware tool/resource handlers can default-resolve their `session`
    arg or reject pin violations
  * `ServerSession.__aenter__` can capture the pin onto the session
    instance (via the broadcaster's monkey-patch path) so the bus → MCP
    fan-out can filter notifications per-client

Connections that don't include `?session=` keep the prior global behavior
— admin / diagnostic mode. Existing unscoped `.mcp.json` files keep
working untouched.
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qs

# Minimal ASGI3 typing — keeps `Mount(app=PinExtractionMiddleware(...))`
# happy without depending on a Starlette internal alias.
_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]

# Per-request: set by the middleware, read inside tool/resource handlers
# to default-resolve `session` args and reject pin violations.
currentPin: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "taskmux_mcp_pin", default=None
)


class PinViolation(Exception):
    """Raised when a pinned client requests a session that doesn't match."""

    def __init__(self, pinned_session: str, requested_session: str) -> None:
        self.pinned_session = pinned_session
        self.requested_session = requested_session
        super().__init__(
            f"this MCP connection is pinned to session {pinned_session!r}; "
            f"cannot access {requested_session!r}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "error": "pin_violation",
            "message": str(self),
            "pinned_session": self.pinned_session,
            "requested_session": self.requested_session,
        }


def resolveSession(arg: str | None) -> str | None:
    """Default-resolve a tool's `session` arg against the current pin.

    Cases:
      * pinned + arg None    → return the pin
      * pinned + arg matches → return arg (= pin)
      * pinned + arg differs → raise `PinViolation`
      * unpinned + arg None  → return None (caller decides whether the
                                            tool requires session and
                                            errors on missing)
      * unpinned + arg given → return arg verbatim
    """
    pin = currentPin.get()
    if pin is None:
        return arg
    if arg is None or arg == pin:
        return pin
    raise PinViolation(pinned_session=pin, requested_session=arg)


def _extractSessionFromQuery(query_string: bytes | str) -> str | None:
    if isinstance(query_string, bytes):
        query_string = query_string.decode("latin-1")
    if not query_string:
        return None
    params = parse_qs(query_string)
    values = params.get("session")
    if not values:
        return None
    pin = values[0].strip()
    return pin or None


class PinExtractionMiddleware:
    """ASGI middleware: read `?session=` from URL, populate the contextvar.

    Wraps the FastMCP `streamable_http_app` inside the daemon's parent
    Starlette mount. Every request flows through here. Non-HTTP scopes
    (lifespan, websocket) are passed through unchanged.

    The middleware sets/resets the contextvar around the inner call so
    handlers see the pin only for their own request — concurrent
    differently-pinned clients can't cross-pollinate.
    """

    def __init__(self, app: _ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        pin = _extractSessionFromQuery(scope.get("query_string", b""))
        token = currentPin.set(pin)
        try:
            await self.app(scope, receive, send)
        finally:
            currentPin.reset(token)
