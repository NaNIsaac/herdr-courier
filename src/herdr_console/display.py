"""Text sized by terminal cells, for output read on a phone (62 columns in Termux portrait)."""

import shutil
import unicodedata

PHONE_WIDTH = 62
ELLIPSIS = "…"


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
