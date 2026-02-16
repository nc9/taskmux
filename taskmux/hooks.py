"""Lifecycle hook execution for Taskmux."""

import subprocess


def runHook(hook_cmd: str | None, task_name: str | None = None) -> bool:
    """Run a hook command. Returns True on success or if no hook defined."""
    if hook_cmd is None:
        return True

    label = f"[{task_name}] " if task_name else ""
    print(f"{label}Running hook: {hook_cmd}")

    try:
        result = subprocess.run(
            hook_cmd,
            shell=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            print(f"{label}Hook failed (exit {result.returncode}): {hook_cmd}")
            if result.stderr.strip():
                print(result.stderr.strip())
            return False
    except subprocess.TimeoutExpired:
        print(f"{label}Hook timed out (30s): {hook_cmd}")
        return False
    except Exception as e:
        print(f"{label}Hook error: {e}")
        return False

    return True
