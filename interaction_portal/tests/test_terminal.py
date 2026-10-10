"""Unit tests for terminal color utilities and ANSI stripping fallback."""

from __future__ import annotations

import io
import os
import pytest

from app.terminal import (
    AnsiFilterStream,
    bold,
    cyan,
    dim,
    green,
    init_terminal,
    red,
    strip_ansi,
    supports_color,
    yellow,
)


def test_strip_ansi_basic():
    raw = "\x1b[32mINFO\x1b[0m:     \x1b[36m127.0.0.1:8000\x1b[0m - \"\x1b[1mGET / HTTP/1.1\x1b[0m\" \x1b[32m200 OK\x1b[0m"
    cleaned = strip_ansi(raw)
    assert "\x1b[" not in cleaned
    assert "←[" not in cleaned
    assert cleaned == 'INFO:     127.0.0.1:8000 - "GET / HTTP/1.1" 200 OK'


def test_strip_ansi_empty_and_plain():
    assert strip_ansi("") == ""
    assert strip_ansi("plain text without color") == "plain text without color"


def test_supports_color_no_color_env(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert supports_color() is False


def test_supports_color_force_color_env(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert supports_color() is True


def test_supports_color_non_tty():
    buf = io.StringIO()
    # StringIO is not a tty
    os_env_clean = not os.environ.get("FORCE_COLOR")
    if os_env_clean:
        assert supports_color(buf) is False


def test_colorize_when_color_disabled(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    assert green("success") == "success"
    assert red("failure") == "failure"
    assert cyan("info") == "info"
    assert bold("heading") == "heading"
    assert yellow("warning") == "warning"
    assert dim("details") == "details"


def test_colorize_when_color_enabled(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    assert green("success") == "\x1b[32msuccess\x1b[0m"
    assert red("failure") == "\x1b[31mfailure\x1b[0m"
    assert cyan("info") == "\x1b[36minfo\x1b[0m"
    assert bold("heading") == "\x1b[1mheading\x1b[0m"
    assert yellow("warning") == "\x1b[33mwarning\x1b[0m"


def test_ansi_filter_stream():
    target = io.StringIO()
    filter_stream = AnsiFilterStream(target)

    # Write text with ANSI escape codes
    filter_stream.write("\x1b[32mPASS\x1b[0m - test completed successfully\n")
    filter_stream.writelines(["\x1b[1mLine 1\x1b[0m\n", "\x1b[36mLine 2\x1b[0m\n"])
    filter_stream.flush()

    output = target.getvalue()
    assert "\x1b[" not in output
    assert "PASS - test completed successfully\n" in output
    assert "Line 1\n" in output
    assert "Line 2\n" in output


def test_init_terminal_execution():
    result = init_terminal(safe_fallback=True)
    assert isinstance(result, bool)
