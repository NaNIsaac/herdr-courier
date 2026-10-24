"""Decide whether an agent's input box holds human text, from an ANSI pane read.

Claude Code draws its input box between two horizontal rules, with the first line starting
with the prompt glyph. Placeholders (tips, "Press up to edit queued messages", suggested next
prompts) are drawn dim (SGR 2); typed text is drawn without SGR. A plain-text read cannot tell
the two apart. Anything that does not look like that box is UNKNOWN, so a renderer change or an
unrecognised dialog makes the dispatcher wait instead of typing into it.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

SGR = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESCAPE = re.compile(r"\x1b\[[0-9;?:]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
RULE = "─" * 8
PROMPT = "❯"


class LineState(StrEnum):
    EMPTY = "empty"
    TYPED = "typed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InputLine:
    state: LineState
    text: str
    evidence: str


def _styled(line: str) -> list[tuple[str, bool]]:
    """Characters of one line paired with whether they are drawn dim."""
    out: list[tuple[str, bool]] = []
    dim = False
    pos = 0
    for m in SGR.finditer(line):
        out += [(c, dim) for c in OTHER_ESCAPE.sub("", line[pos : m.start()])]
        params = m.group(1).replace(":", ";").split(";")
        i = 0
        while i < len(params):
            p = params[i] or "0"
            if p in ("38", "48", "58"):
                # Extended colour: skip its arguments so a "2" there is not read as dim.
                i += 5 if i + 1 < len(params) and params[i + 1] == "2" else 3
                continue
            if p in ("0", "22"):
                dim = False
            elif p == "2":
                dim = True
            i += 1
        pos = m.end()
    out += [(c, dim) for c in OTHER_ESCAPE.sub("", line[pos:])]
    return out


def _plain(line: str) -> str:
    return SGR.sub("", OTHER_ESCAPE.sub("", line))


def classify(screen: str) -> InputLine:
    lines = screen.splitlines()
    plain = [_plain(ln) for ln in lines]
    rules = [i for i, ln in enumerate(plain) if ln.strip().startswith(RULE)]
    if len(rules) < 2:
        return InputLine(LineState.UNKNOWN, "", "no input box (fewer than two rules)")
    top, bottom = rules[-2], rules[-1]
    # Blank rows under the top rule hold no input; some consoles redraw with one there.
    while top + 1 < bottom and not plain[top + 1].strip():
        top += 1
    body = lines[top + 1 : bottom]
    if not body or not plain[top + 1].lstrip().startswith(PROMPT):
        shown = " | ".join(plain[top + 1 : bottom])[:80]
        return InputLine(LineState.UNKNOWN, "", f"box does not start with {PROMPT}: {shown!r}")
    first = _styled(body[0])
    start = next(i for i, (c, _) in enumerate(first) if c == PROMPT) + 1
    chars = first[start:]
    for ln in body[1:]:
        chars += [("\n", False), *_styled(ln)]
    text = "".join(c for c, _ in chars).strip()
    ink = [dim for c, dim in chars if not c.isspace()]
    if not ink:
        return InputLine(LineState.EMPTY, "", "prompt line blank")
    if all(ink):
        return InputLine(LineState.EMPTY, "", f"dim placeholder {text[:60]!r}")
    return InputLine(LineState.TYPED, text, f"typed {text[:60]!r}")


def holds(line: InputLine, sent: str) -> bool:
    """True when the box still shows the start of a message we submitted."""
    if line.state is not LineState.TYPED:
        return False
    head = " ".join(sent.split())[:24]
    return bool(head) and head in " ".join(line.text.split())
