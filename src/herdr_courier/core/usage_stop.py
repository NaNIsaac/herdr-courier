"""Recognise a Claude Code usage stop: the last reply above its input box.

A Codex limit is not held here: `inputline.rejection` ends that message as rejected.
"""

import re

PREFIXES = (
    "You're out of usage credits",
    "You're out of extra usage",
    "Your org is out of usage",
    "You've hit your limit",
    "You've hit your monthly limit",
    "You've hit your monthly spend limit",
    "You've hit your channel's monthly spend limit",
    "Credit balance is too low",
)
TAIL = 15
RULE = "─" * 8
# Box drawing, the reply and result marks Claude Code puts before a line, and bullets.
LEAD = re.compile(r"^[\s─-╿⎿⏺●•·▪◦*\-]+")
# A prompt echoed above the box: anything below it answers that prompt, so an older stop line
# above it no longer holds, as after someone switched the model.
ECHO = ("> ", "❯ ")


def _box_top(lines: list[str]) -> int:
    """Index of the live input box's first line, or ``len(lines)`` when no box is seen."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("❯"):
            return i - 1 if i and RULE in lines[i - 1] else i
    return len(lines)


def _stop(line: str) -> bool:
    if line.startswith(PREFIXES):
        return True
    _, found, after = line.partition("usage limit reached")
    return bool(found) and "send a message after it resets" in after


def usage_stop(screen: str) -> str | None:
    """The stop line among the last TAIL non-empty lines above the input box, else None."""
    lines = screen.splitlines()
    seen = 0
    for raw in reversed(lines[: _box_top(lines)]):
        if not raw.strip():
            continue
        seen += 1
        if seen > TAIL or raw.lstrip().startswith(ECHO):
            break
        line = LEAD.sub("", raw)
        if _stop(line.replace("’", "'")):
            return line[:120]
    return None
