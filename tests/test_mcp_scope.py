"""Tests for taskmux.mcp.scope (pin extraction + resolution)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from taskmux.mcp import scope
from taskmux.mcp.scope import (
    PinExtractionMiddleware,
    PinViolation,
    _extractSessionFromQuery,
    currentPin,
    resolveSession,
)


@pytest.fixture(autouse=True)
def resetPinContextVar() -> None:
    """Belt-and-braces — every test starts with the contextvar default."""
    if currentPin.get() is not None:
        # ContextVars don't auto-reset across tests; clear by setting a
        # token-less default for the test scope.
        currentPin.set(None)


# ---- _extractSessionFromQuery ----


def testEmptyQueryReturnsNone() -> None:
    assert _extractSessionFromQuery(b"") is None
    assert _extractSessionFromQuery("") is None


def testQueryWithoutSessionKeyReturnsNone() -> None:
    assert _extractSessionFromQuery(b"foo=bar") is None
    assert _extractSessionFromQuery(b"limit=50&task=api") is None


def testQueryWithSessionReturnsValue() -> None:
    assert _extractSessionFromQuery(b"session=taskmux") == "taskmux"
    assert _extractSessionFromQuery("session=demo&other=1") == "demo"


def testQueryWithEmptySessionReturnsNone() -> None:
    """`?session=` with no value isn't a meaningful pin — treat as unpinned."""
    assert _extractSessionFromQuery(b"session=") is None
    assert _extractSessionFromQuery(b"session=   ") is None


def testQueryWithMultipleSessionTakesFirst() -> None:
    """Pathological but defined — first value wins."""
    assert _extractSessionFromQuery(b"session=a&session=b") == "a"


# ---- resolveSession matrix ----


def testResolveUnpinnedArgNoneReturnsNone() -> None:
    assert resolveSession(None) is None


def testResolveUnpinnedArgGivenPassesThrough() -> None:
    assert resolveSession("foo") == "foo"


def testResolvePinnedArgNoneReturnsPin() -> None:
    token = currentPin.set("taskmux")
    try:
        assert resolveSession(None) == "taskmux"
    finally:
        currentPin.reset(token)


def testResolvePinnedArgMatchReturnsPin() -> None:
    token = currentPin.set("taskmux")
    try:
        assert resolveSession("taskmux") == "taskmux"
    finally:
        currentPin.reset(token)


def testResolvePinnedArgMismatchRaises() -> None:
    token = currentPin.set("taskmux")
    try:
        with pytest.raises(PinViolation) as excinfo:
            resolveSession("domaingenius")
        assert excinfo.value.pinned_session == "taskmux"
        assert excinfo.value.requested_session == "domaingenius"
        payload = excinfo.value.to_dict()
        assert payload["error"] == "pin_violation"
        assert payload["pinned_session"] == "taskmux"
        assert payload["requested_session"] == "domaingenius"
    finally:
        currentPin.reset(token)


# ---- PinExtractionMiddleware ----


class _CapturingApp:
    """Fake ASGI app that records what the contextvar looks like during the call."""

    def __init__(self) -> None:
        self.observed: list[str | None] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.observed.append(currentPin.get())


def _httpScope(query: bytes) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": query,
        "headers": [],
    }


async def _noop(*_args: Any, **_kwargs: Any) -> None:  # receive/send stub
    return None


def testMiddlewareSetsPinDuringRequest() -> None:
    async def run() -> None:
        inner = _CapturingApp()
        mw = PinExtractionMiddleware(inner)
        await mw(_httpScope(b"session=demo"), _noop, _noop)
        assert inner.observed == ["demo"]
        # Reset on exit — outside the call, the var is back to None.
        assert currentPin.get() is None

    asyncio.run(run())


def testMiddlewareUnpinnedRequestSetsNone() -> None:
    async def run() -> None:
        inner = _CapturingApp()
        mw = PinExtractionMiddleware(inner)
        await mw(_httpScope(b""), _noop, _noop)
        assert inner.observed == [None]

    asyncio.run(run())


def testMiddlewareNonHttpScopePassesThrough() -> None:
    """Lifespan/websocket scopes shouldn't touch the contextvar."""

    async def run() -> None:
        inner = _CapturingApp()
        mw = PinExtractionMiddleware(inner)
        token = currentPin.set("taskmux")
        try:
            await mw({"type": "lifespan"}, _noop, _noop)
        finally:
            currentPin.reset(token)
        # Lifespan scope sees the pre-existing pin; middleware didn't override.
        assert inner.observed == ["taskmux"]

    asyncio.run(run())


def testMiddlewareResetsPinOnInnerException() -> None:
    """A throwing inner app must not leak the pin to the next request."""

    class _Boom:
        async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("kaboom")

    async def run() -> None:
        mw = PinExtractionMiddleware(_Boom())
        with pytest.raises(RuntimeError):
            await mw(_httpScope(b"session=demo"), _noop, _noop)
        assert currentPin.get() is None

    asyncio.run(run())


# Internal symbols imported for completeness — silence unused-import guards.
_ = scope
