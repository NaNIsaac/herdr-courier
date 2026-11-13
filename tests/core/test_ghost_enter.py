from herdr_courier import mailbox
from herdr_courier.core.inputline import InputLine, LineState, matches
from herdr_courier.core.queue import (
    FOCUSED_READER_GRACE,
    GHOST_ENTER_SECONDS,
    GHOST_ENTERS,
    State,
    pointer,
)


def typed(text):
    return InputLine(LineState.TYPED, text, "test")


def test_a_message_the_box_wrapped_onto_more_rows_is_still_ours():
    sent = "merge-ready RL#24 at 0123abcd, tests 52 passed"
    assert matches(typed("merge-ready RL#24 at 0123abcd,\n  tests 52 passed"), sent)
    assert matches(typed("merge-ready RL#24 at 0123ab\n  cd, tests 52 passed"), sent)
    assert not matches(typed("merge-ready RL#24 at 0123abcd, tests 53 passed"), sent)
    assert not matches(typed("merge-ready RL#24 at 0123abcd,\n  tests 52 passed and more"), sent)


def test_a_paste_placeholder_with_the_message_line_count_is_ours():
    sent = "\n".join(f"line {n}" for n in range(34))
    assert matches(typed("[Pasted text #13 +33 lines]"), sent)
    assert not matches(typed("[Pasted text #13 +5 lines]"), sent)
    assert not matches(typed("note [Pasted text #13 +33 lines]"), sent)


def ghost_in_box(herdr, make_dispatcher, run, **kw):
    pane = herdr.add("w1:p1", "lane", submit_mode="stuck")
    d = make_dispatcher(**kw)
    item = d.enqueue("lane", "report", source="t")
    run(d, 20)
    assert item.state is State.GHOST and pane.box == "report"
    return pane, d, item


def test_courier_presses_enter_again_on_its_own_text_until_it_takes(herdr, make_dispatcher, run):
    pane, d, item = ghost_in_box(herdr, make_dispatcher, run)
    first = pane.enter_calls

    def take(_pane_id):
        pane.enter_calls += 1
        pane.submitted.append(pane.box)
        pane.box = ""

    herdr.enter = take
    run(d, GHOST_ENTER_SECONDS + 10)
    assert pane.enter_calls == first + 1
    assert pane.submitted == ["report"]
    assert item.state is State.SENT


def test_enter_is_not_pressed_again_on_a_focused_pane_or_on_edited_text(
    herdr, make_dispatcher, run
):
    pane, d, item = ghost_in_box(herdr, make_dispatcher, run)
    first = pane.enter_calls
    pane.focused = True
    run(d, GHOST_ENTER_SECONDS + 10)
    pane.focused = False
    pane.box = "report, and what I am typing"
    run(d, (GHOST_ENTERS + 1) * GHOST_ENTER_SECONDS)
    assert pane.enter_calls == first
    assert item.state is State.GHOST


def test_a_pointer_left_in_a_readers_box_gets_enter_again(herdr, make_dispatcher, run, tmp_path):
    pane = herdr.add("w1:p1", "boss", submit_mode="stuck")
    d = make_dispatcher(pointer_for=("boss",))
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 20)
    assert item.state is State.FILED and pane.box == pointer(item)
    first = pane.enter_calls
    run(d, GHOST_ENTER_SECONDS + 10)
    assert pane.enter_calls == first + 1


def test_a_focused_idle_reader_with_an_empty_box_gets_its_pointer_after_a_grace(
    herdr, make_dispatcher, run, tmp_path
):
    pane = herdr.add("w1:p1", "boss", focused=True)
    d = make_dispatcher(pointer_for=("boss",))
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, FOCUSED_READER_GRACE - 5)
    assert pane.submit_calls == 0 and item.state is State.WAITING
    run(d, 15)
    assert pane.submitted == [pointer(item)]
    assert item.state is State.FILED
    mailbox.mark(tmp_path, "boss", [item.id], "shown", "UserPromptSubmit")
    run(d, 5)
    assert item.state is State.SENT


def test_a_focused_reader_with_text_in_its_box_gets_nothing_typed(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "boss", focused=True, box="owner typing")
    d = make_dispatcher(pointer_for=("boss",))
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, FOCUSED_READER_GRACE + 20)
    assert item.state is State.FILED and item.reason.startswith("in the mailbox for the hook")
    assert pane.submit_calls == 0


def test_a_pointer_left_in_a_focused_readers_box_gets_enter_again(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "boss", submit_mode="stuck", focused=True)
    d = make_dispatcher(pointer_for=("boss",))
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, FOCUSED_READER_GRACE + 20)
    assert pane.box == pointer(item)
    first = pane.enter_calls
    run(d, GHOST_ENTER_SECONDS + 10)
    assert pane.enter_calls == first + 1
