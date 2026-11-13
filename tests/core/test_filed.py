import os

from herdr_courier import mailbox
from herdr_courier.core.queue import (
    IDLE_WAKE_SECONDS,
    REWAKE_SECONDS,
    REWAKES,
    Dispatcher,
    State,
    pointer,
)


def reader(herdr, clock, tmp_path, **kw):
    return Dispatcher(
        herdr,
        tmp_path / "journal.jsonl",
        clock=clock,
        wall=clock,
        log=lambda _: None,
        pointer_for=("boss",),
        **kw,
    )


def test_a_typed_pointer_is_sent_only_once_the_hook_has_shown_it(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p1", "boss")
    d = reader(herdr, clock, tmp_path)
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 10)
    assert pane.submitted == [pointer(item)]
    assert item.state is State.FILED
    mailbox.mark(tmp_path, "boss", [item.id], "shown", "UserPromptSubmit")
    run(d, 5)
    assert item.state is State.SENT
    assert item.reason == "shown to boss by its hook"


def test_an_unshown_message_is_woken_again_a_few_times_then_left_filed(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p1", "boss")
    d = reader(herdr, clock, tmp_path)
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 10)
    for _ in range(REWAKES + 2):
        # an agent that finished its turn: the Stop hook had nothing to bring then
        pane.status = "idle"
        run(d, REWAKE_SECONDS + 10)
    assert pane.submitted == [pointer(item)] * (1 + REWAKES)
    assert item.state is State.FILED


def test_a_message_filed_for_a_working_reader_gets_its_pointer_soon_after_it_goes_idle(
    herdr, clock, tmp_path, run
):
    pane = herdr.add("w1:p1", "boss", status="working", box="half typed")
    d = reader(herdr, clock, tmp_path)
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 10)
    assert item.state is State.FILED and pane.submit_calls == 0
    pane.status, pane.box = "idle", ""
    run(d, IDLE_WAKE_SECONDS + 10)
    assert pane.submitted == [pointer(item)]
    assert item.state is State.FILED
    mailbox.mark(tmp_path, "boss", [item.id], "shown", "UserPromptSubmit")
    run(d, 5)
    assert item.state is State.SENT


def test_a_reader_that_never_worked_waits_for_the_usual_rewake(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p1", "boss", box="half typed")
    d = reader(herdr, clock, tmp_path)
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 10)
    assert item.state is State.FILED
    run(d, IDLE_WAKE_SECONDS + 30)
    assert pane.submit_calls == 0 and item.state is State.FILED


def test_a_filed_message_stays_filed_across_a_restart(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p1", "boss", status="working")
    first = reader(herdr, clock, tmp_path)
    item = first.enqueue("boss", "courier: a notice", source="courier")
    run(first, 10)
    assert item.state is State.FILED and pane.submit_calls == 0
    second = reader(herdr, clock, tmp_path)
    assert second.items[item.id].state is State.FILED
    mailbox.mark(tmp_path, "boss", [item.id], "shown", "Stop")
    run(second, 5)
    assert second.items[item.id].state is State.SENT


def test_messages_for_others_still_wait_for_an_empty_box(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p2", "lane", box="someone is typing")
    d = reader(herdr, clock, tmp_path)
    item = d.enqueue("lane", "report", source="t")
    run(d, 10)
    assert item.state is State.WAITING and pane.submit_calls == 0


def waiting(tmp_path, clock):
    """A reader's `courier inbox --wait` that touched its file just now."""
    path = mailbox.wait_path(tmp_path, "boss")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("token", encoding="utf-8")
    os.utime(path, (clock(), clock()))


def test_a_reader_with_a_live_waiter_gets_no_pointer(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p1", "boss")
    d = reader(herdr, clock, tmp_path)
    waiting(tmp_path, clock)
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 2)
    assert item.state is State.FILED and pane.submit_calls == 0
    mailbox.mark(tmp_path, "boss", [item.id], "shown", "Stop/wait")
    run(d, 5)
    assert item.state is State.SENT and pane.submit_calls == 0


def test_a_message_its_waiter_ended_without_gets_its_pointer(herdr, clock, tmp_path, run):
    pane = herdr.add("w1:p1", "boss")
    d = reader(herdr, clock, tmp_path)
    waiting(tmp_path, clock)
    item = d.enqueue("boss", "courier: a notice", source="courier")
    run(d, 2)
    assert item.state is State.FILED and pane.submit_calls == 0
    run(d, mailbox.WAITER_FRESH + 10)
    assert pane.submitted == [pointer(item)]
    assert item.state is State.FILED
