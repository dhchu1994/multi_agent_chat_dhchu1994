"""Interaction Portal package."""

from .terminal import init_terminal

# Ensure Windows terminal colors (Anaconda Prompt, cmd.exe, PowerShell) are initialized
# or fallback to stripping escape sequences if colors are not supported.
init_terminal()
