"""URL building for proxy-routed tasks."""

from __future__ import annotations


def taskUrl(project: str, host: str, scheme: str = "https") -> str:
    """Build the proxy URL for a task.

    - `host == ""`  → `{scheme}://{project}.localhost`        (apex)
    - `host == "*"` → `{scheme}://*.{project}.localhost`      (wildcard,
       display only — wildcards aren't a real hostname you can curl)
    - otherwise     → `{scheme}://{host}.{project}.localhost` (specific)
    """
    if host == "":
        return f"{scheme}://{project}.localhost"
    return f"{scheme}://{host}.{project}.localhost"


def taskUrlPath(project: str, host: str, path: str, scheme: str = "https") -> str:
    """Build a full URL with a path."""
    base = taskUrl(project, host, scheme)
    if not path.startswith("/"):
        path = "/" + path
    return base + path
