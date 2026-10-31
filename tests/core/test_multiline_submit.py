import json

import pytest

from herdr_courier.core.inputline import InputLine, LineState, matches
from herdr_courier.core.queue import Dispatcher, State
from herdr_courier.core.transport import HerdrError


def test_dropped_multiline_enter_is_retried_without_repasting(
    herdr, make_dispatcher, run, monkeypatch
):
    pane = herdr.add("w1:p1", submit_mode="stuck")
    keys = []

    def enter(pane_id):
        keys.append(pane_id)
        pane.submitted.append(pane.box)
        pane.box = ""
        pane.status = "working"

    monkeypatch.setattr(herdr, "enter", enter, raising=False)
    dispatcher = make_dispatcher()
    text = "First line\nSecond line"
    item = dispatcher.enqueue("w1:p1", text, source="test")
    run(dispatcher, 15)
    assert item.state is State.SENT
    assert pane.submitted == [text]
    assert pane.submit_calls == 1
    assert keys == ["w1:p1"]


@pytest.fixture
def pending(herdr, make_dispatcher, run, monkeypatch):
    pane = herdr.add("w1:p1", submit_mode="stuck", terminal="term-1")
    keys = []

    def enter(pane_id):
        keys.append(pane_id)
        pane.submitted.append(pane.box)
        pane.box = ""
        pane.status = "working"
        pane.submit_mode = "ok"

    monkeypatch.setattr(herdr, "enter", enter, raising=False)
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1:p1", "A complete first line\nSecond line", source="test")
    run(dispatcher, 2)
    assert item.state is State.VERIFYING
    return pane, dispatcher, item, keys


def test_retry_unblocks_the_following_queue_without_duplicate_text(pending, run):
    pane, dispatcher, first, keys = pending
    second = dispatcher.enqueue("w1:p1", "Follow-up", source="test")
    run(dispatcher, 15)
    assert first.state is second.state is State.SENT
    assert pane.submitted == [first.text, "Follow-up"]
    assert pane.submit_calls == 2
    assert len(keys) == 1


def test_retry_is_bounded_when_enter_still_does_not_take(pending, run, herdr, monkeypatch):
    pane, dispatcher, item, keys = pending
    monkeypatch.setattr(herdr, "enter", keys.append)
    run(dispatcher, 60)
    assert item.state is State.FAILED
    assert "one Enter retry" in item.reason
    assert pane.box == item.text
    assert pane.submit_calls == 1
    assert len(keys) == 1


@pytest.mark.parametrize(
    "change", ["focused", "hold", "working", "session", "terminal", "dialog", "edit", "suffix"]
)
def test_retry_never_submits_a_human_edit_or_changed_target(pending, run, change):
    pane, dispatcher, item, keys = pending
    if change == "focused":
        pane.focused = True
    elif change == "hold":
        dispatcher.hold("w1:p1", owner="test", reason="typing", ttl=None)
    elif change == "working":
        pane.status = "working"
    elif change == "session":
        pane.session = "another-session"
    elif change == "terminal":
        pane.terminal = "another-terminal"
    elif change == "dialog":
        pane.dialog = True
        pane.status = "blocked"
    elif change == "edit":
        pane.box = "A complete first line\nAn edited second line"
    else:
        pane.box += "\nHuman draft"
    original = pane.box
    run(dispatcher, 15)
    assert not keys
    assert pane.box == original
    assert pane.submit_calls == 1
    if change == "working":
        assert item.state is State.FAILED


def test_hold_arriving_during_retry_observation_is_honoured(
    pending, herdr, clock, run, monkeypatch
):
    _, dispatcher, item, keys = pending
    original = herdr.agent_get
    reads = []

    def get(target):
        if clock() >= item.verify_until:
            reads.append(target)
            if len(reads) == 2:
                dispatcher.hold("w1:p1", owner="test", reason="late hold", ttl=None)
        return original(target)

    monkeypatch.setattr(herdr, "agent_get", get)
    run(dispatcher, 15)
    assert item.state is State.FAILED
    assert not keys


def test_empty_box_at_final_check_confirms_delivery_without_enter(
    pending, herdr, clock, run, monkeypatch
):
    pane, dispatcher, item, keys = pending
    original = herdr.agent_get
    reads = []

    def get(target):
        if clock() >= item.verify_until:
            reads.append(target)
            if len(reads) == 2:
                pane.box = ""
        return original(target)

    monkeypatch.setattr(herdr, "agent_get", get)
    run(dispatcher, 15)
    assert item.state is State.SENT
    assert not keys


def test_uncertain_enter_error_is_never_retried(pending, herdr, run, monkeypatch):
    _, dispatcher, item, keys = pending

    def enter(pane_id):
        keys.append(pane_id)
        raise HerdrError("cli_timeout", "unknown key outcome")

    monkeypatch.setattr(herdr, "enter", enter)
    run(dispatcher, 30)
    assert item.state is State.FAILED
    assert len(keys) == 1


def test_journal_failure_prevents_the_recovery_key(pending, run, monkeypatch):
    _, dispatcher, item, keys = pending
    original = dispatcher._journal

    def journal(entry):
        if entry.get("enter_retried"):
            raise OSError("disk full")
        original(entry)

    monkeypatch.setattr(dispatcher, "_journal", journal)
    run(dispatcher, 20)
    assert not keys
    assert item.state is State.FAILED


def test_restart_during_enter_never_replays_the_key_or_paste(
    pending, herdr, clock, tmp_path, run, monkeypatch
):
    pane, dispatcher, item, keys = pending

    def enter(pane_id):
        keys.append(pane_id)
        records = [
            json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()
        ]
        assert records[-1]["enter_retried"] is True
        raise SystemExit("simulated crash")

    monkeypatch.setattr(herdr, "enter", enter)
    with pytest.raises(SystemExit):
        run(dispatcher, 15)
    restored = Dispatcher(herdr, tmp_path / "journal.jsonl", clock=clock, log=lambda _: None)
    run(restored, 30)
    assert restored.items[item.id].state is State.FAILED
    assert item.id in restored._enter_retried
    assert len(keys) == pane.submit_calls == 1


@pytest.mark.parametrize(
    ("visible", "message", "expected"),
    [
        ("First\n  Second", "First\nSecond", True),
        ("First\n  Second", "First\r\nSecond", True),
        ("First\n  Second edited", "First\nSecond", False),
        ("First\n      code", "First\n    code", True),
        ("First\n    code", "First\n    code", False),
    ],
)
def test_complete_match_preserves_indentation_and_line_endings(visible, message, expected):
    assert matches(InputLine(LineState.TYPED, visible, "test"), message) is expected


def test_long_multiline_verification_reads_enough_of_the_box(
    herdr, make_dispatcher, run, monkeypatch
):
    pane = herdr.add("w1:p1", submit_mode="stuck")
    keys, windows = [], []

    def read(pane_id, lines=12):
        windows.append(lines)
        return "\n".join(pane.screen().splitlines()[-lines:])

    def enter(pane_id):
        keys.append(pane_id)
        pane.box = ""
        pane.status = "working"

    monkeypatch.setattr(herdr, "read_ansi", read)
    monkeypatch.setattr(herdr, "enter", enter, raising=False)
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1:p1", "\n".join(f"Line {n}" for n in range(30)), source="test")
    run(dispatcher, 15)
    assert item.state is State.SENT
    assert keys == ["w1:p1"]
    assert min(windows) >= 36
    assert max(windows) <= 256
