"""Helpers for turning raw terminal output into readable text."""

from __future__ import annotations

import re

# CSI sequences, OSC sequences (terminated by BEL or ST) and two-byte escapes.
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_OTHER_CONTROL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def last_visible_line(text: str, max_chars: int = 300) -> str | None:
    """Return the last non-blank line as a terminal would display it.

    Carriage returns (used by progress bars) overwrite the start of the line, so
    the visible content is the last non-empty ``\\r``-separated segment.
    """
    cleaned = strip_ansi(text).replace("\r\n", "\n")
    for raw_line in reversed(cleaned.split("\n")):
        for segment in reversed(raw_line.split("\r")):
            visible = _OTHER_CONTROL.sub("", segment).strip()
            if visible:
                return visible[:max_chars]
    return None
