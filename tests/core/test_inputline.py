from pathlib import Path

import pytest

from herdr_courier.core.inputline import LineState, classify, holds

DATA = Path(__file__).parent / "data"
RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 30 + "\x1b[0m"


def box(line: str) -> str:
    return f"output\r\n{RULE}\r\n{line}\r\n{RULE}\r\n  footer\r\n"


# Input boxes captured from a Claude Code pane (third-party UI text, no conversation content)
# with `herdr pane read --source recent --format ansi`; the typed text is test input.
@pytest.mark.parametrize(
    ("name", "state", "text"),
    [
        ("claude_empty_after_turn", LineState.EMPTY, ""),
        ("claude_cleared", LineState.EMPTY, ""),
        ("claude_typed", LineState.TYPED, "isaac typing"),
        ("claude_multiline", LineState.TYPED, "isaac typing\n  second line"),
        ("claude_cursor_on_placeholder", LineState.EMPTY, ""),
        ("claude_typed_unfocused", LineState.TYPED, "isaac typing"),
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


@pytest.mark.parametrize(
    ("line", "text"),
    [
        ("❯ isaac typing\x1b[7m \x1b[0m", "isaac typing"),
        ("❯ \x1b[7mi\x1b[0msaac typing", "isaac typing"),
        ("❯ isaac \x1b[7mt\x1b[0myping", "isaac typing"),
        ("❯ \x1b[7mx\x1b[0m", "x"),
        ("❯ /com\x1b[7mp\x1b[0m\x1b[2mact\x1b[0m", "/compact"),
        ("❯ \x1b[7mab\x1b[27m\x1b[2mc suggestion\x1b[0m", "abc suggestion"),
    ],
)
def test_typed_text_under_cursor_stays_typed(line, text):
    got = classify(box(line))
    assert got.state is LineState.TYPED
    assert got.text == text


def test_cursor_on_dim_placeholder_is_empty():
    line = classify(box('❯ \x1b[0m\x1b[7mT\x1b[0m\x1b[2mry "refactor <filepath>"\x1b[0m'))
    assert line.state is LineState.EMPTY
    assert "placeholder" in line.evidence


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


def test_blank_rows_above_prompt_are_skipped():
    screen = f"out\r\n{RULE}\r\n\r\n❯ half typed\r\n\r\n{RULE}\r\n"
    line = classify(screen)
    assert line.state is LineState.TYPED
    assert line.text == "half typed"


def test_box_of_blank_rows_only_is_unknown():
    assert classify(f"{RULE}\r\n\r\n\r\n{RULE}\r\n").state is LineState.UNKNOWN


@pytest.mark.parametrize(
    ("line", "state"),
    [
        ("❯ \x1b[38:2::200:200:200mtyped in colour\x1b[0m", LineState.TYPED),
        ("❯ \x1b[38:2::2:2:2mtyped in colour\x1b[0m", LineState.TYPED),
        ("❯ \x1b[2;38:2::9:9:9msuggestion\x1b[0m", LineState.EMPTY),
        ("❯ \x1b[38;5;2mtyped\x1b[0m", LineState.TYPED),
        ("❯ \x1b[4:3;2msuggestion\x1b[0m", LineState.EMPTY),
    ],
)
def test_colour_arguments_are_not_read_as_dim(line, state):
    assert classify(box(line)).state is state
