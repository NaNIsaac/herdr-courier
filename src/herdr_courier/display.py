"""Text sized by terminal cells, for output read on a phone (62 columns in Termux portrait)."""

import os
import shutil
import unicodedata
from typing import TextIO

PHONE_WIDTH = 62
ELLIPSIS = "…"
COLOR_CHOICES = ("auto", "always", "never")


def color_wanted(when: str, stream: TextIO) -> bool:
    """``--color`` as in ls and grep; ``auto`` colours a terminal unless NO_COLOR is set (no-color.org)."""
    if when != "auto":
        return when == "always"
    return not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" and stream.isatty()


def paint(cell: str, sgr: str) -> str:
    """Wrap the text of a fitted cell in SGR and leave its padding outside, so columns stay aligned."""
    body = cell.rstrip(" ")
    if not sgr or not body:
        return cell
    return f"\x1b[{sgr}m{body}\x1b[0m{cell[len(body) :]}"


def cols(text: str) -> int:
    """Display columns: East Asian wide and fullwidth characters take two cells (Korean tab labels)."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def fit(text: str, width: int) -> str:
    """Pad or cut ``text`` to exactly ``width`` cells, marking a cut with an ellipsis."""
    if width <= 0:
        return ""
    if cols(text) <= width:
        return text + " " * (width - cols(text))
    if width == 1:
        return ELLIPSIS
    out, used = [], 0
    for c in text:
        w = cols(c)
        if used + w > width - 1:
            break
        out.append(c)
        used += w
    kept = "".join(out).rstrip()
    return kept + ELLIPSIS + " " * (width - 1 - cols(kept))


def clip(text: str, width: int) -> str:
    """``text`` on one line of at most ``width`` cells, without padding."""
    return fit(" ".join(text.split()), width).rstrip()


def clip_left(text: str, width: int) -> str:
    """Keep the end of ``text`` (the useful part of a path) within ``width`` cells."""
    if cols(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS[:width]
    out, used = [], 0
    for c in reversed(text):
        w = cols(c)
        if used + w > width - 1:
            break
        out.append(c)
        used += w
    return ELLIPSIS + "".join(reversed(out))


def wrap(text: str, width: int, first: str, rest: str) -> list[str]:
    """Greedy word wrap by display columns; textwrap counts code points, not cells.

    ``first`` and ``rest`` are kept verbatim, so a prefix can carry aligned columns. A word longer
    than a whole line is cut with an ellipsis rather than left to overflow.
    """
    lines: list[str] = []
    current, prefix = "", first
    for word in text.split():
        candidate = f"{current} {word}" if current else prefix + word
        if current and cols(candidate) > width:
            lines.append(current)
            prefix = rest
            current = prefix + word
        else:
            current = candidate
    if current or not lines:
        lines.append(current or first.rstrip())
    return [fit(line, width).rstrip() if cols(line) > width else line for line in lines]


def terminal_width() -> int:
    return shutil.get_terminal_size((PHONE_WIDTH, 24)).columns
