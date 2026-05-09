"""Tests for `taskmux env` rendering — quoting, prefix, task var normalisation."""

from __future__ import annotations

import json

import pytest

from taskmux.env_export import (
    SUPPORTED_SHELLS,
    normalizeTaskVar,
    renderEnv,
    renderEnvJson,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("api", "API"),
        ("web-1", "WEB_1"),
        ("frontloader", "FRONTLOADER"),
        ("hyphen-laden-task", "HYPHEN_LADEN_TASK"),
        ("with.dot", "WITH_DOT"),
        ("12leading", "12LEADING"),  # we don't enforce var-name shape on suffix
    ],
)
def test_normalize_task_var(name, expected):
    assert normalizeTaskVar(name) == expected


def test_normalize_empty_falls_back_to_task():
    assert normalizeTaskVar("") == "TASK"
    assert normalizeTaskVar("---") == "TASK"


def _baseKwargs():
    return {
        "project": "postpiece",
        "project_id": "postpiece-feature-foo",
        "branch": "feature/foo",
        "worktree": "feature-foo",
        "is_linked": True,
        "tasks": [("api", "api"), ("website", ""), ("frontloader", "*"), ("mcp", "mcp")],
    }


def test_render_posix_default_prefix_full_payload():
    # shlex.quote leaves [A-Za-z0-9@%+=:,./-]+ values unquoted — that's POSIX-safe.
    out = renderEnv(**_baseKwargs(), shell="posix")
    assert "export TASKMUX_PROJECT=postpiece" in out
    assert "export TASKMUX_PROJECT_ID=postpiece-feature-foo" in out
    assert "export TASKMUX_BASE_HOST=postpiece-feature-foo.localhost" in out
    assert "export TASKMUX_BRANCH=feature/foo" in out
    assert "export TASKMUX_WORKTREE=feature-foo" in out
    assert "export TASKMUX_IS_LINKED=1" in out
    assert "export TASKMUX_URL_API=https://api.postpiece-feature-foo.localhost" in out
    assert "export TASKMUX_URL_WEBSITE=https://postpiece-feature-foo.localhost" in out
    assert "export TASKMUX_URL_MCP=https://mcp.postpiece-feature-foo.localhost" in out


def test_render_skips_wildcard_host():
    out = renderEnv(**_baseKwargs(), shell="posix")
    assert "FRONTLOADER" not in out
    assert "*." not in out


def test_render_primary_omits_branch_and_worktree_when_none():
    kw = _baseKwargs()
    kw.update(branch=None, worktree=None, is_linked=False, project_id="postpiece")
    out = renderEnv(**kw, shell="posix")
    assert "TASKMUX_BRANCH" not in out
    assert "TASKMUX_WORKTREE" not in out
    assert "export TASKMUX_IS_LINKED=0" in out
    assert "export TASKMUX_BASE_HOST=postpiece.localhost" in out


def test_render_no_urls_flag():
    out = renderEnv(**_baseKwargs(), shell="posix", include_urls=False)
    assert "TASKMUX_URL_" not in out
    assert "TASKMUX_PROJECT_ID" in out


def test_render_prefix_replaces_default():
    out = renderEnv(**_baseKwargs(), shell="posix", prefix="POSTPIECE_")
    assert "TASKMUX_" not in out
    assert "export POSTPIECE_PROJECT_ID=postpiece-feature-foo" in out
    assert "export POSTPIECE_URL_API=https://api.postpiece-feature-foo.localhost" in out


def test_render_empty_prefix():
    out = renderEnv(**_baseKwargs(), shell="posix", prefix="")
    assert "export PROJECT_ID=postpiece-feature-foo" in out
    assert "export URL_API=https://api.postpiece-feature-foo.localhost" in out


def test_render_fish_uses_set_gx():
    out = renderEnv(**_baseKwargs(), shell="fish")
    assert "set -gx TASKMUX_PROJECT_ID 'postpiece-feature-foo'" in out
    # No POSIX `export` syntax leaks through.
    assert "\nexport " not in out


def test_render_fish_quotes_special_chars():
    kw = _baseKwargs()
    # branch with a single quote — fish backslash-escapes it.
    kw["branch"] = "feat/it's-fine"
    out = renderEnv(**kw, shell="fish")
    assert r"set -gx TASKMUX_BRANCH 'feat/it\'s-fine'" in out


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_render_supported_shells_emit_something(shell):
    out = renderEnv(**_baseKwargs(), shell=shell)
    assert "TASKMUX_PROJECT_ID" in out
    assert out.endswith("\n")


def test_render_unsupported_shell_raises():
    with pytest.raises(ValueError, match="unsupported shell"):
        renderEnv(**_baseKwargs(), shell="csh")


def test_json_render_payload_shape():
    raw = renderEnvJson(**_baseKwargs(), prefix="MYPROJ_")
    payload = json.loads(raw)
    assert payload["MYPROJ_PROJECT"] == "postpiece"
    assert payload["MYPROJ_PROJECT_ID"] == "postpiece-feature-foo"
    assert payload["MYPROJ_BASE_HOST"] == "postpiece-feature-foo.localhost"
    assert payload["MYPROJ_BRANCH"] == "feature/foo"
    assert payload["MYPROJ_WORKTREE"] == "feature-foo"
    assert payload["MYPROJ_IS_LINKED"] == "1"
    assert payload["MYPROJ_URL_API"] == "https://api.postpiece-feature-foo.localhost"
    # Wildcard-host tasks excluded.
    assert "MYPROJ_URL_FRONTLOADER" not in payload


def test_json_render_omits_optional_fields_when_none():
    kw = _baseKwargs()
    kw.update(branch=None, worktree=None, is_linked=False, project_id="postpiece")
    payload = json.loads(renderEnvJson(**kw))
    assert "TASKMUX_BRANCH" not in payload
    assert "TASKMUX_WORKTREE" not in payload
    assert payload["TASKMUX_IS_LINKED"] == "0"


def test_render_apex_host_uses_base_host():
    """Tasks with host="" should resolve to the apex (base host), not collapse."""
    out = renderEnv(
        project="postpiece",
        project_id="postpiece",
        branch=None,
        worktree=None,
        is_linked=False,
        tasks=[("website", "")],
        shell="posix",
    )
    assert "export TASKMUX_URL_WEBSITE=https://postpiece.localhost" in out
