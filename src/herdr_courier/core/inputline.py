"""Decide whether an agent's input box holds human text, from an ANSI pane read.

Claude Code draws its input box between two horizontal rules, with the first line starting
with the prompt glyph. Placeholders (tips, "Press up to edit queued messages", suggested next
prompts) are drawn dim (SGR 2); typed text is drawn without SGR. A plain-text read cannot tell
the two apart. The cursor can be drawn as one reverse-video cell (SGR 7) over the first character
of a placeholder, so that one cell is not dim. Anything that does not look like that box is
UNKNOWN, so a renderer change or an unrecognised dialog makes the dispatcher wait instead of
typing into it.

Antigravity CLI (agy) also draws its input box between two horizontal rules, starting with "> ".
Its placeholder ("Auto mode: edits & safe commands auto-approved (shift+tab to cycle)") is drawn
dim (SGR 2 or muted grey SGR 38;2;154;160;166).

Codex draws no rules. Its input is a line starting with "›", padded above and below by a blank
row with a background colour (SGR 48), and its placeholder ("Ask Codex to do anything") is dim.
The same "›" marks the selected option of a dialog, but there the rows around it are not blank
padding, so only a flanked line counts as the input line. Newer Codex builds draw the input
without a background: the lowest "›" line on the screen, between two plain blank rows, is then
the input line, one row only. A dialog option has another option or text next to it, and typed
text over more than one row is UNKNOWN until it is sent or cleared.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

SGR = re.compile(r"\x1b\[([0-9;:]*)m")
OTHER_ESCAPE = re.compile(r"\x1b\[[0-9;?:]*[A-Za-ln-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
RULE = "─" * 8
PROMPT = "❯"
AGY_PROMPT = ">"
AGY_DIM_FG = (154, 160, 166)
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
    fg: tuple[int, int, int] | None = None


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
    dim, reverse, fg = style.dim, style.reverse, style.fg
    groups = params.split(";")
    i = 0
    while i < len(groups):
        head = groups[i].split(":")[0] or "0"
        if ":" in groups[i]:
            # Colon form (38:2::R:G:B, 4:3) carries its own arguments in the same group.
            if head == "0":
                dim = reverse = False
                fg = None
            elif head == "22":
                dim = False
            elif head == "38":
                parts = groups[i].split(":")
                if len(parts) >= 5 and parts[1] == "2" and all(p.isdigit() for p in parts[-3:]):
                    fg = (int(parts[-3]), int(parts[-2]), int(parts[-1]))
                else:
                    fg = None
            elif head == "39":
                fg = None
            i += 1
            continue
        if head in ("38", "48", "58"):
            # Semicolon form: 38;5;N or 38;2;R;G;B, so a "2" there is not read as dim.
            mode = groups[i + 1] if i + 1 < len(groups) else ""
            if head == "38":
                if (
                    mode == "2"
                    and i + 4 < len(groups)
                    and all(g.isdigit() for g in groups[i + 2 : i + 5])
                ):
                    fg = (int(groups[i + 2]), int(groups[i + 3]), int(groups[i + 4]))
                else:
                    fg = None
            i += 5 if mode == "2" else 3 if mode == "5" else 2
            continue
        if head == "0":
            dim = reverse = False
            fg = None
        elif head == "22":
            dim = False
        elif head == "2":
            dim = True
        elif head == "7":
            reverse = True
        elif head == "27":
            reverse = False
        elif head == "39" or (head.isdigit() and (30 <= int(head) <= 37 or 90 <= int(head) <= 97)):
            fg = None
        i += 1
    return _Style(dim, reverse, fg)


def _box_prompt(line: str) -> str | None:
    stripped = line.lstrip()
    if stripped.startswith(PROMPT):
        return PROMPT
    if stripped == AGY_PROMPT or stripped.startswith((f"{AGY_PROMPT} ", f"{AGY_PROMPT}\xa0")):
        return AGY_PROMPT
    return None


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
    return _classify_plain_codex(lines, plain)


def _classify_plain_codex(lines: list[str], plain: list[str]) -> InputLine:
    at = next(
        (i for i in range(len(lines) - 1, -1, -1) if plain[i].lstrip().startswith(CODEX_PROMPT)),
        None,
    )
    if at is not None:
        styled = _styled(lines[at])
        start = next(i for i, (c, _) in enumerate(styled) if c == CODEX_PROMPT) + 1
        chars = styled[start:]
        text = "".join(c for c, _ in chars).strip()
        ink = [style for c, style in chars if not c.isspace()]
        footer = plain[at + 1 :]
        if (
            text == "Ask Codex to do anything"
            and _placeholder(ink)
            and (at == 0 or plain[at - 1].strip() or at + 1 < len(plain) and plain[at + 1].strip())
            and any("? for shortcuts" in row for row in footer)
            and not any(re.match(r"\s*(?:[›>]\s*)?\d+[.)]\s", row) for row in footer)
        ):
            return InputLine(LineState.EMPTY, "", "codex dim placeholder with shortcuts footer")
    if (
        at is None
        or at == 0
        or plain[at - 1].strip()
        or (at + 1 < len(lines) and plain[at + 1].strip())
    ):
        return InputLine(
            LineState.UNKNOWN,
            "",
            f"no input box (no rules, no {CODEX_PROMPT} line between padding or blank rows)",
        )
    first = _styled(lines[at])
    start = next(i for i, (c, _) in enumerate(first) if c == CODEX_PROMPT) + 1
    chars = first[start:]
    text = "".join(c for c, _ in chars).strip()
    ink = [style for c, style in chars if not c.isspace()]
    if not ink:
        return InputLine(LineState.EMPTY, "", "codex prompt line blank (no background)")
    if _placeholder(ink):
        return InputLine(
            LineState.EMPTY, "", f"codex dim placeholder {text[:60]!r} (no background)"
        )
    return InputLine(LineState.TYPED, text, f"codex typed {text[:60]!r} (no background)")


def _owned_codex(plain: list[str], queued: str, unknown: InputLine) -> InputLine:
    at = next(
        (i for i in range(len(plain) - 1, -1, -1) if plain[i].lstrip().startswith(CODEX_PROMPT)),
        None,
    )
    if at is None or at == 0 or plain[at - 1].strip():
        return unknown
    end = next((i for i in range(at + 1, len(plain)) if not plain[i].strip()), None)
    if end is None:
        return unknown
    body = [plain[at].lstrip()[1:].lstrip(), *plain[at + 1 : end]]
    if any(re.match(r"\s*\d+[.)]\s", row) for row in body):
        return unknown
    if any(not row.startswith("  ") for row in plain[at + 1 : end]):
        return unknown
    line = InputLine(LineState.TYPED, "\n".join(body).strip(), "codex complete queued multiline")
    return line if matches(line, queued) else unknown


def classify(screen: str, *, queued: str | None = None) -> InputLine:
    lines = screen.splitlines()
    plain = [_plain(ln) for ln in lines]
    rules = [i for i, ln in enumerate(plain) if ln.strip().startswith(RULE)]
    if len(rules) < 2:
        line = _classify_codex(lines, plain)
        return (
            _owned_codex(plain, queued, line)
            if (queued and line.state is LineState.UNKNOWN)
            else line
        )
    top, bottom = rules[-2], rules[-1]
    # Blank rows under the top rule hold no input; some consoles redraw with one there.
    while top + 1 < bottom and not plain[top + 1].strip():
        top += 1
    body = lines[top + 1 : bottom]
    prompt = _box_prompt(plain[top + 1]) if body else None
    if prompt is None:
        shown = " | ".join(plain[top + 1 : bottom])[:80]
        return InputLine(LineState.UNKNOWN, "", f"box does not start with {PROMPT}: {shown!r}")
    first = _styled(body[0])
    start = next(i for i, (c, _) in enumerate(first) if c == prompt) + 1
    chars = first[start:]
    for ln in body[1:]:
        chars += [("\n", _Style()), *_styled(ln)]
    text = "".join(c for c, _ in chars).strip()
    ink = [style for c, style in chars if not c.isspace()]
    if prompt == AGY_PROMPT:
        ink = [_Style(s.dim or s.fg == AGY_DIM_FG, s.reverse) for s in ink]
    if not ink:
        return InputLine(LineState.EMPTY, "", "prompt line blank")
    if _placeholder(ink):
        return InputLine(LineState.EMPTY, "", f"dim placeholder {text[:60]!r}")
    return InputLine(LineState.TYPED, text, f"typed {text[:60]!r}")


_PASTED = re.compile(r"\[Pasted text #\d+ \+(\d+) lines\]")


def holds(line: InputLine, sent: str) -> bool:
    """True when the box still shows the start of a message we submitted."""
    if line.state is not LineState.TYPED:
        return False
    head = " ".join(sent.split())[:24]
    return bool(head) and head in " ".join(line.text.split())


def matches(line: InputLine, sent: str) -> bool:
    """Prove complete ownership; a shared prefix is insufficient for a recovery keystroke.

    The box may show the message wrapped onto more rows than it has lines, which only moves
    whitespace, or, for a long paste, as one ``[Pasted text #N +K lines]`` placeholder whose K
    fits the message's line count."""
    if line.state is not LineState.TYPED:
        return False
    text = sent.replace("\r\n", "\n").strip()
    if not text:
        return False
    if " ".join(line.text.split()) == " ".join(text.split()):
        return True
    pasted = _PASTED.fullmatch(line.text.strip())
    return bool(pasted) and int(pasted.group(1)) in (text.count("\n"), text.count("\n") + 1)


def rejection(screen: str) -> str | None:
    """Only a current Codex error outside its input, not quoted/user/history text."""
    rows = [_plain(row) for row in screen.splitlines()]
    prompts = [at for at, row in enumerate(rows) if row.lstrip().startswith(CODEX_PROMPT)]
    if not prompts:
        return None
    end = prompts[-1]
    start = prompts[-2] + 1 if len(prompts) > 1 else 0
    for row in rows[start:end]:
        if re.match(r"^\s*(?:■\s*)?You(?:[’']ve| have) hit your usage limit\b", row, re.IGNORECASE):
            context = "\n".join(rows[max(0, start - 1) : end])
            evidence = sha256(context.encode()).hexdigest()[:12]
            return f"codex_usage_limit: receiver reports its usage limit ({evidence})"
    return None
