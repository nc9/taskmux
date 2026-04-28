"""Tests for the URL builder — apex + wildcard + specific shapes."""

from __future__ import annotations

import pytest

from taskmux.url import taskUrl, taskUrlPath


@pytest.mark.parametrize(
    ("project", "host", "expected"),
    [
        # Specific subdomain — original behaviour.
        ("postpiece", "api", "https://api.postpiece.localhost"),
        ("postpiece", "web-1", "https://web-1.postpiece.localhost"),
        # Apex — empty host renders without a leading dot.
        ("postpiece", "", "https://postpiece.localhost"),
        # Wildcard — display form for status output. Caller can't curl it,
        # but it's the canonical user-visible representation.
        ("postpiece", "*", "https://*.postpiece.localhost"),
        # Worktree project_id composes naturally for all three shapes.
        ("postpiece-fix-bug", "", "https://postpiece-fix-bug.localhost"),
        ("postpiece-fix-bug", "*", "https://*.postpiece-fix-bug.localhost"),
        ("postpiece-fix-bug", "api", "https://api.postpiece-fix-bug.localhost"),
    ],
)
def test_task_url_shapes(project, host, expected):
    assert taskUrl(project, host) == expected


def test_task_url_custom_scheme():
    assert taskUrl("p", "api", scheme="http") == "http://api.p.localhost"
    assert taskUrl("p", "", scheme="http") == "http://p.localhost"


def test_task_url_path_apex_and_wildcard():
    assert taskUrlPath("p", "", "/health") == "https://p.localhost/health"
    assert taskUrlPath("p", "*", "/api") == "https://*.p.localhost/api"
    # Path normalisation: missing leading slash gets one.
    assert taskUrlPath("p", "", "health") == "https://p.localhost/health"
