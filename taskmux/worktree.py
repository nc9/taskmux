"""Git worktree detection and worktree-id composition.

When taskmux is invoked inside a linked git worktree, the project gets a
distinct `project_id = {project_name}-{worktree_id}` so each worktree has
its own session, state, ports, cert, and URL namespace.

Detection: linked iff `git rev-parse --git-dir` != `--git-common-dir`.
Worktree id derivation:
  1. Branch via `git symbolic-ref --short HEAD` → last `/`-segment.
  2. Sanitize: lowercase, [a-z0-9-] only, collapse runs, strip ends.
  3. If sanitized empty, branch ∈ main_branches, or detached HEAD → fall
     back to last path segment of the worktree dir.
  4. Truncate to 63 chars (RFC 1035) with `-{6char_sha1}` suffix when
     truncation occurs.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NON_DNS = re.compile(r"[^a-z0-9-]+")
_RUNS = re.compile(r"-{2,}")
_DNS_LABEL_MAX = 63

DEFAULT_MAIN_BRANCHES: tuple[str, ...] = ("main", "master")


@dataclass(frozen=True)
class WorktreeInfo:
    """Result of `detectWorktree`. Always attached to a real git checkout."""

    path: Path
    """Canonical path of the worktree (or repo root for primary)."""
    primary_path: Path
    """Canonical path of the primary worktree (the one bound to the .git dir)."""
    is_linked: bool
    """True when this worktree is linked, not the primary."""
    branch: str | None
    """Symbolic branch name (full ref, slashes intact). None on detached HEAD."""
    head_sha: str | None
    """Short HEAD sha (used for detached-HEAD fallback)."""


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a git command silently. Returns stdout (stripped) or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def detectWorktree(cwd: Path | None = None) -> WorktreeInfo | None:
    """Detect git worktree state for a directory. Returns None if not in a repo
    (or git is missing)."""
    base = (cwd or Path.cwd()).resolve()
    git_dir = _git(["rev-parse", "--git-dir"], base)
    if git_dir is None:
        return None
    common_dir = _git(["rev-parse", "--git-common-dir"], base)
    top_level = _git(["rev-parse", "--show-toplevel"], base)
    if top_level is None:
        return None

    git_dir_p = Path(git_dir)
    common_dir_p = Path(common_dir) if common_dir else git_dir_p
    if not git_dir_p.is_absolute():
        git_dir_p = (base / git_dir_p).resolve()
    if not common_dir_p.is_absolute():
        common_dir_p = (base / common_dir_p).resolve()

    is_linked = git_dir_p != common_dir_p

    branch = _git(["symbolic-ref", "--short", "HEAD"], base)
    head_sha = _git(["rev-parse", "--short", "HEAD"], base)

    primary_path = _primary_worktree_path(base, common_dir_p, top_level, is_linked)

    return WorktreeInfo(
        path=Path(top_level).resolve(),
        primary_path=primary_path,
        is_linked=is_linked,
        branch=branch or None,
        head_sha=head_sha or None,
    )


def _primary_worktree_path(cwd: Path, common_dir: Path, top_level: str, is_linked: bool) -> Path:
    """Resolve the primary worktree path.

    For a primary checkout this is just `top_level`. For a linked worktree
    we parse `git worktree list --porcelain` and pick the first entry — that's
    always the primary in git's bookkeeping.
    """
    if not is_linked:
        return Path(top_level).resolve()
    raw = _git(["worktree", "list", "--porcelain"], cwd)
    if raw:
        for block in raw.split("\n\n"):
            for line in block.splitlines():
                if line.startswith("worktree "):
                    return Path(line[len("worktree ") :]).resolve()
    # Fallback: the .git common dir's parent is usually the primary worktree.
    return common_dir.parent.resolve()


def _sanitize(value: str, max_len: int = _DNS_LABEL_MAX) -> str:
    """Lowercase, replace non-DNS chars with `-`, collapse runs, strip ends.
    Truncate + append 6-char sha1 when over max_len."""
    s = _NON_DNS.sub("-", value.lower())
    s = _RUNS.sub("-", s).strip("-")
    if not s:
        return ""
    if len(s) > max_len:
        digest = hashlib.sha1(value.encode()).hexdigest()[:6]  # noqa: S324
        s = s[: max_len - 7].rstrip("-") + "-" + digest
    return s


def slugifyBranch(branch: str) -> str:
    """Branch → DNS-safe slug. Takes last `/`-segment, sanitizes, truncates."""
    last = branch.rsplit("/", 1)[-1]
    return _sanitize(last)


def computeWorktreeId(
    info: WorktreeInfo,
    main_branches: tuple[str, ...] | list[str] = DEFAULT_MAIN_BRANCHES,
) -> str | None:
    """Derive the worktree_id slug, or None when no suffix should be applied.

    Rules:
      - Primary worktree → None.
      - Linked + branch present + branch not in main_branches → slug the branch
        (last `/`-segment).
      - Linked + branch in main_branches OR detached HEAD OR slug empty → slug
        the worktree dir basename.
      - If any sibling worktree of the same primary repo would yield the same
        candidate, append `-{6char_sha1(path)}` to disambiguate.
    """
    if not info.is_linked:
        return None

    main_set = set(main_branches)
    candidate = ""
    if info.branch and info.branch not in main_set:
        candidate = slugifyBranch(info.branch)

    if not candidate:
        candidate = _sanitize(info.path.name)

    if not candidate and info.head_sha:
        candidate = _sanitize(f"sha-{info.head_sha}")

    if not candidate:
        return None

    if _candidate_collides_with_siblings(info, candidate, main_branches):
        digest = hashlib.sha1(str(info.path).encode()).hexdigest()[:6]  # noqa: S324
        # Stay within RFC 1035 even after suffixing.
        max_prefix = _DNS_LABEL_MAX - 7
        candidate = candidate[:max_prefix].rstrip("-") + "-" + digest

    return candidate or None


def _candidate_collides_with_siblings(
    info: WorktreeInfo,
    candidate: str,
    main_branches: tuple[str, ...] | list[str],
) -> bool:
    """True if any sibling linked worktree (same primary) would yield `candidate`.

    Walks `git worktree list --porcelain` and re-derives the slug for each
    other worktree using the same rules (without recursion). Cheap: bounded
    by the number of worktrees on the repo, called once per identity load.
    """
    raw = _git(["worktree", "list", "--porcelain"], info.path)
    if not raw:
        return False
    main_set = set(main_branches)
    for block in raw.split("\n\n"):
        wt_path: Path | None = None
        wt_branch: str | None = None
        is_detached = False
        for line in block.splitlines():
            if line.startswith("worktree "):
                wt_path = Path(line[len("worktree ") :]).resolve()
            elif line.startswith("branch "):
                # Format: `branch refs/heads/<name>`
                ref = line[len("branch ") :]
                wt_branch = ref.removeprefix("refs/heads/")
            elif line.strip() == "detached":
                is_detached = True
        if wt_path is None:
            continue
        if wt_path == info.path:
            continue
        # Skip the primary worktree — only compare against linked siblings.
        if wt_path == info.primary_path:
            continue
        sibling_candidate = ""
        if wt_branch and not is_detached and wt_branch not in main_set:
            sibling_candidate = slugifyBranch(wt_branch)
        if not sibling_candidate:
            sibling_candidate = _sanitize(wt_path.name)
        if sibling_candidate == candidate:
            return True
    return False


def composeProjectId(
    project_name: str,
    worktree_id: str | None,
    separator: str = "-",
) -> str:
    """`project_name` for primary, `project_name{sep}{worktree_id}` for linked."""
    if not worktree_id:
        return project_name
    return f"{project_name}{separator}{worktree_id}"
