"""
Terminal and Console Color Support Utility.

Enables ANSI color sequences in Windows consoles (cmd.exe, Anaconda Prompt, PowerShell)
and provides a safe fallback:
- If colors are supported/enabled: renders output with full terminal colors.
- If colors are NOT supported: strips ANSI escape sequences (such as \\x1b[32m, \\x1b[0m),
  preventing raw display of control characters like ←[32m, ←[0m, ←[36m, ←[1m.
"""

from __future__ import annotations

import io
import os
import re
import sys
from typing import Any, TextIO

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([a-zA-Z]")


def strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from a string."""
    if not text:
        return ""
    return ANSI_RE.sub("", text)


class AnsiFilterStream(io.TextIOBase):
    """A stream wrapper that intercepts writes and strips ANSI escape sequences.
    
    Used as a fallback when the console does not support ANSI colors, ensuring
    raw escape sequences like ←[32m never appear in the terminal output.
    """

    def __init__(self, target: TextIO) -> None:
        self._target = target

    def write(self, s: str) -> int:
        if not s:
            return 0
        cleaned = strip_ansi(s)
        return self._target.write(cleaned)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        if hasattr(self._target, "flush"):
            self._target.flush()

    def isatty(self) -> bool:
        return getattr(self._target, "isatty", lambda: False)()

    @property
    def encoding(self) -> str:
        return getattr(self._target, "encoding", "utf-8")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def _enable_windows_vt() -> bool:
    """Attempt to enable ENABLE_VIRTUAL_TERMINAL_PROCESSING on Windows via ctypes."""
    if sys.platform != "win32":
        return True

    success = False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

        # 1. Try standard handles (STD_OUTPUT_HANDLE = -11, STD_ERROR_HANDLE = -12)
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            if handle and handle != -1:
                mode = ctypes.c_ulong()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    if not (mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                        new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                        if kernel32.SetConsoleMode(handle, new_mode):
                            success = True
                    else:
                        success = True

        # 2. Try CONOUT$ directly for Windows console screen buffer
        try:
            conout_name = "CONOUT$"
            h_conout = kernel32.CreateFileW(
                conout_name,
                0x40000000 | 0x80000000,  # GENERIC_READ | GENERIC_WRITE
                2,                       # FILE_SHARE_WRITE
                None,
                3,                       # OPEN_EXISTING
                0,
                None,
            )
            if h_conout and h_conout != -1:
                mode = ctypes.c_ulong()
                if kernel32.GetConsoleMode(h_conout, ctypes.byref(mode)):
                    if not (mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                        if kernel32.SetConsoleMode(h_conout, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                            success = True
                    else:
                        success = True
                kernel32.CloseHandle(h_conout)
        except Exception:
            pass
    except Exception:
        pass

    return success


def supports_color(stream: Any = None) -> bool:
    """Check if the given stream (defaults to sys.stdout) supports ANSI colors.
    
    Respects standard environment variables:
    - NO_COLOR: if set (non-empty), disables color.
    - FORCE_COLOR: if set to 1/true, forces color.
    """
    # 1. Environment variable overrides
    if os.environ.get("NO_COLOR"):
        return False
    force = os.environ.get("FORCE_COLOR", "").lower()
    if force in ("1", "true", "yes", "on"):
        return True

    target = stream or sys.stdout
    if not hasattr(target, "isatty") or not target.isatty():
        return False

    # 2. Windows-specific console inspection
    if sys.platform == "win32":
        # Windows Terminal always supports VT colors
        if os.environ.get("WT_SESSION"):
            return True
        # ANSICON or ConEmu
        if os.environ.get("ANSICON") or os.environ.get("ConEmuANSI") == "ON":
            return True
        # Terminal emulation
        term = os.environ.get("TERM", "")
        if term in ("xterm", "xterm-256color", "vt100", "rxvt"):
            return True
        # Check active console mode for ENABLE_VIRTUAL_TERMINAL_PROCESSING
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return bool(mode.value & 0x0004)
        except Exception:
            pass
        return False

    # 3. Unix-like terminals
    term = os.environ.get("TERM", "")
    if term == "dumb":
        return False
    return True


_INITIALIZED = False


def init_terminal(safe_fallback: bool = True) -> bool:
    """Initialize terminal color support for Windows (cmd.exe, Anaconda Prompt, PowerShell).
    
    1. Attempts to enable colorama if available (standard in Anaconda environments).
    2. Attempts to enable Win32 ENABLE_VIRTUAL_TERMINAL_PROCESSING via ctypes.
    3. Evaluates if color output is supported.
    4. Safe fallback: if colors are NOT supported and safe_fallback=True, wraps
       sys.stdout and sys.stderr with an AnsiFilterStream so any ANSI sequences
       emitted by libraries (such as uvicorn) are stripped, avoiding raw ←[32m artifacts.
    
    Returns True if color output is enabled/supported, False otherwise.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return supports_color()
    _INITIALIZED = True

    # 1. Try colorama (if installed, e.g. Anaconda Prompt)
    colorama_ok = False
    try:
        import colorama
        if hasattr(colorama, "just_fix_windows_console"):
            colorama.just_fix_windows_console()
            colorama_ok = True
        else:
            colorama.init()
            colorama_ok = True
    except Exception:
        pass

    # 2. Try native Win32 VT processing via ctypes
    vt_ok = _enable_windows_vt()

    color_possible = supports_color() or colorama_ok or vt_ok

    # 3. Fallback: if colors cannot be rendered, filter ANSI escape sequences
    if not color_possible and safe_fallback:
        if not isinstance(sys.stdout, AnsiFilterStream):
            sys.stdout = AnsiFilterStream(sys.stdout)  # type: ignore[assignment]
        if not isinstance(sys.stderr, AnsiFilterStream):
            sys.stderr = AnsiFilterStream(sys.stderr)  # type: ignore[assignment]

    return color_possible


# ---------------------------------------------------------------------------
# Formatting helpers (automatically omit escape sequences if colors unavailable)
# ---------------------------------------------------------------------------

def colorize(text: str, code: str, stream: Any = None) -> str:
    """Apply an ANSI color escape code if colors are supported; otherwise return plain text."""
    if not supports_color(stream):
        return strip_ansi(text)
    return f"\x1b[{code}m{text}\x1b[0m"


def green(text: str) -> str:
    return colorize(text, "32")


def red(text: str) -> str:
    return colorize(text, "31")


def yellow(text: str) -> str:
    return colorize(text, "33")


def blue(text: str) -> str:
    return colorize(text, "34")


def magenta(text: str) -> str:
    return colorize(text, "35")


def cyan(text: str) -> str:
    return colorize(text, "36")


def bold(text: str) -> str:
    return colorize(text, "1")


def dim(text: str) -> str:
    return colorize(text, "2")
