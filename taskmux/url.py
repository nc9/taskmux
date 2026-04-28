"""URL building for proxy-routed tasks."""

from __future__ import annotations


def taskUrl(project: str, host: str, scheme: str = "https") -> str:
    """Build the proxy URL for a task: {scheme}://{host}.{project}.localhost"""
    return f"{scheme}://{host}.{project}.localhost"


def taskUrlPath(project: str, host: str, path: str, scheme: str = "https") -> str:
    """Build a full URL with a path."""
    base = taskUrl(project, host, scheme)
    if not path.startswith("/"):
        path = "/" + path
    return base + path
