"""Lifecycle hook execution for Taskmux."""

import subprocess

from .errors import ErrorCode, TaskmuxError
from .output import is_json_mode, print_error

HOOK_TIMEOUT = 30


def runHook(hook_cmd: str | None, task_name: str | None = None, *, quiet: bool = False) -> bool:
    """Run a hook command. Returns True on success or if no hook defined."""
    if hook_cmd is None:
        return True

    label = f"[{task_name}] " if task_name else ""
    if not quiet and not is_json_mode():
        print(f"{label}Running hook: {hook_cmd}")

    try:
        result = subprocess.run(
            hook_cmd,
            shell=True,
            timeout=HOOK_TIMEOUT,
            capture_output=True,
            text=True,
        )
        if not quiet and not is_json_mode() and result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            if not quiet:
                err = TaskmuxError(
                    ErrorCode.HOOK_FAILED,
                    exit_code=result.returncode,
                    command=hook_cmd,
                )
                print_error(err)
                if not is_json_mode() and result.stderr.strip():
                    print(result.stderr.strip())
            return False
    except subprocess.TimeoutExpired:
        if not quiet:
            print_error(
                TaskmuxError(ErrorCode.HOOK_TIMEOUT, timeout=HOOK_TIMEOUT, command=hook_cmd)
            )
        return False
    except Exception as e:
        if not quiet:
            print_error(TaskmuxError(ErrorCode.INTERNAL, detail=f"Hook error: {e}"))
        return False

    return True
