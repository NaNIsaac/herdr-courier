from pathlib import Path

import pytest

from herdr_console.inputline import LineState, classify, holds

DATA = Path(__file__).parent / "data"
RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 30 + "\x1b[0m"


def box(line: str) -> str:
    return f"output\r\n{RULE}\r\n{line}\r\n{RULE}\r\n  footer\r\n"


# Captured from a Claude Code pane with `herdr pane read --source recent --format ansi`.
@pytest.mark.parametrize(
    ("name", "state", "text"),
    [
        ("claude_empty_after_turn", LineState.EMPTY, ""),
        ("claude_cleared", LineState.EMPTY, ""),
        ("claude_typed", LineState.TYPED, "isaac typing"),
        ("claude_multiline", LineState.TYPED, "isaac typing\n  second line"),
    ],
)
def test_captured_screens(name, state, text):
    line = classify((DATA / f"{name}.ansi").read_text(encoding="utf-8"))
    assert line.state is state
    assert line.text == text


@pytest.mark.parametrize(
    "placeholder",
    [
        "\x1b[0m\x1b[38;2;153;153;153m❯ \x1b[0m\x1b[2mPress up to edit queued messages\x1b[0m",
        "❯ \x1b[0m\x1b[2m[boss] suggested next prompt\x1b[0m",
        '❯ \x1b[2mTry "how does <filepath> work?"\x1b[22m',
    ],
)
def test_dim_placeholder_is_empty(placeholder):
    line = classify(box(placeholder))
    assert line.state is LineState.EMPTY
    assert "placeholder" in line.evidence


def test_truecolor_with_component_two_is_not_dim():
    line = classify(box("❯ \x1b[38;2;2;200;2mtyped in colour\x1b[0m"))
    assert line.state is LineState.TYPED


def test_partly_dim_counts_as_typed():
    line = classify(box("❯ abc\x1b[2m suggestion tail\x1b[0m"))
    assert line.state is LineState.TYPED


def test_dialog_without_box_is_unknown():
    screen = "Do you trust this folder?\r\n❯ No, exit\r\n  Yes, I trust this folder\r\n"
    assert classify(screen).state is LineState.UNKNOWN


def test_shell_mode_prompt_is_unknown():
    assert classify(box("! ls")).state is LineState.UNKNOWN


def test_uses_last_box_on_screen():
    screen = box("❯ old text") + box("❯ ")
    assert classify(screen).state is LineState.EMPTY


def test_holds_detects_our_text_left_in_box():
    line = classify(box("❯ [lane] status report with   extra spaces"))
    assert holds(line, "[lane] status report with extra spaces")
    assert not holds(line, "something else entirely")
