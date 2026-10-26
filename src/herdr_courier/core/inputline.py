"""Decide whether an agent's input box holds human text, from an ANSI pane read.

Claude Code draws its input box between two horizontal rules, with the first line starting
with the prompt glyph. Placeholders (tips, "Press up to edit queued messages", suggested next
prompts) are drawn dim (SGR 2); typed text is drawn without SGR. A plain-text read cannot tell
the two apart. The cursor can be drawn as one reverse-video cell (SGR 7) over the first character
of a placeholder, so that one cell is not dim. Anything that does not look like that box is
UNKNOWN, so a renderer change or an unrecognised dialog makes the dispatcher wait instead of
typing into it.

Codex draws no rules. Its input is a line starting with "›", padded above and below by a blank
row with a background colour (SGR 48), and its placeholder ("Ask Codex to do anything") is dim.
The same "›" marks the selected option of a dialog, but there the rows around it are not blank
padding, so only a flanked line counts as the input line.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

SGR = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESCAPE = re.compile(r"\x1b\[[0-9;?:]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
RULE = "─" * 8
PROMPT = "❯"
CODEX_PROMPT = "›"
BACKGROUND = re.compile(r"\x1b\[(?:[0-9;:]*;)?(?:48|4[0-7]|10[0-7])[;:m]")


class LineState(StrEnum):
    EMPTY = "empty"
    TYPED = "typed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InputLine:
    state: LineState
    text: str
    evidence: str


@dataclass(frozen=True)
class _Style:
    dim: bool = False
    reverse: bool = False


def _styled(line: str) -> list[tuple[str, _Style]]:
    """Characters of one line paired with how they are drawn."""
    out: list[tuple[str, _Style]] = []
    style = _Style()
    pos = 0
    for m in SGR.finditer(line):
        out += [(c, style) for c in OTHER_ESCAPE.sub("", line[pos : m.start()])]
        style = _apply_sgr(m.group(1), style)
        pos = m.end()
    out += [(c, style) for c in OTHER_ESCAPE.sub("", line[pos:])]
    return out


def _apply_sgr(params: str, style: _Style) -> _Style:
    """Style after one SGR sequence, skipping colour arguments."""
    dim, reverse = style.dim, style.reverse
    groups = params.split(";")
    i = 0
    while i < len(groups):
        head = groups[i].split(":")[0] or "0"
        if ":" in groups[i]:
            # Colon form (38:2::R:G:B, 4:3) carries its own arguments in the same group.
            if head == "0":
                dim = reverse = False
            elif head == "22":
                dim = False
            i += 1
            continue
        if head in ("38", "48", "58"):
            # Semicolon form: 38;5;N or 38;2;R;G;B, so a "2" there is not read as dim.
            mode = groups[i + 1] if i + 1 < len(groups) else ""
            i += 5 if mode == "2" else 3 if mode == "5" else 2
            continue
        if head == "0":
            dim = reverse = False
        elif head == "22":
            dim = False
        elif head == "2":
            dim = True
        elif head == "7":
            reverse = True
        elif head == "27":
            reverse = False
        i += 1
    return _Style(dim, reverse)


def _placeholder(ink: list[_Style]) -> bool:
    """All ink dim, allowing one reverse cursor cell on the first character."""
    if ink and ink[0].reverse and not ink[0].dim and not any(s.reverse for s in ink[1:]):
        # A lone cursor cell with nothing after it may be a typed character.
        ink = ink[1:]
    return bool(ink) and all(s.dim for s in ink)


def _plain(line: str) -> str:
    return SGR.sub("", OTHER_ESCAPE.sub("", line))


def _padding(raw: str, plain: str) -> bool:
    """A blank row drawn on a background colour."""
    return not plain.strip() and bool(BACKGROUND.search(raw))


def _classify_codex(lines: list[str], plain: list[str]) -> InputLine:
    for at in range(len(lines) - 2, 0, -1):
        if not plain[at].lstrip().startswith(CODEX_PROMPT) or not _padding(
            lines[at - 1], plain[at - 1]
        ):
            continue
        end = next((i for i in range(at + 1, len(lines)) if _padding(lines[i], plain[i])), None)
        if end is None:
            continue
        first = _styled(lines[at])
        start = next(i for i, (c, _) in enumerate(first) if c == CODEX_PROMPT) + 1
        chars = first[start:]
        for ln in lines[at + 1 : end]:
            chars += [("\n", _Style()), *_styled(ln)]
        text = "".join(c for c, _ in chars).strip()
        ink = [style for c, style in chars if not c.isspace()]
        if not ink:
            return InputLine(LineState.EMPTY, "", "codex prompt line blank")
        if _placeholder(ink):
            return InputLine(LineState.EMPTY, "", f"codex dim placeholder {text[:60]!r}")
        return InputLine(LineState.TYPED, text, f"codex typed {text[:60]!r}")
    return InputLine(
        LineState.UNKNOWN,
        "",
        f"no input box (no rules, no {CODEX_PROMPT} line between padding rows)",
    )


def classify(screen: str) -> InputLine:
    lines = screen.splitlines()
    plain = [_plain(ln) for ln in lines]
    rules = [i for i, ln in enumerate(plain) if ln.strip().startswith(RULE)]
    if len(rules) < 2:
        return _classify_codex(lines, plain)
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
        chars += [("\n", _Style()), *_styled(ln)]
    text = "".join(c for c, _ in chars).strip()
    ink = [style for c, style in chars if not c.isspace()]
    if not ink:
        return InputLine(LineState.EMPTY, "", "prompt line blank")
    if _placeholder(ink):
        return InputLine(LineState.EMPTY, "", f"dim placeholder {text[:60]!r}")
    return InputLine(LineState.TYPED, text, f"typed {text[:60]!r}")


def holds(line: InputLine, sent: str) -> bool:
    """True when the box still shows the start of a message we submitted."""
    if line.state is not LineState.TYPED:
        return False
    head = " ".join(sent.split())[:24]
    return bool(head) and head in " ".join(line.text.split())
