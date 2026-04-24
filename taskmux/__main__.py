"""Module entry point so `python -m taskmux` works (used by detached daemon spawn)."""

from .cli import main

if __name__ == "__main__":
    main()
