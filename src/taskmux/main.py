#!/usr/bin/env python3
"""
Taskmux - Modern tmux development environment manager

Entry point that delegates to the Typer-based CLI.
"""

from .cli import main

if __name__ == "__main__":
    main()