import json

import pytest

from herdr_console.dispatcher import MAX_ATTEMPTS, Dispatcher, DispatchError, State


def test_waits_while_human_is_typing_and_never_merges(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "boss", box="half typed by isaac ")
    d = make_dispatcher()
    item = d.enqueue("boss", "[lane] status report", source="lane")
    run(d, 10)
    assert item.state is State.WAITING
    assert item.reason.startswith("input typed")
    assert pane.submit_calls == 0

    pane.box = ""
    run(d, 2)
    assert item.state is State.SENT
    assert pane.submitted == ["[lane] status report"]


def test_debounce_is_longer_on_focused_pane(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", focused=True)
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 2)
    assert item.state is State.WAITING
    assert "3 s" in item.reason
    run(d, 2)
    assert item.state is State.SENT
    assert pane.submitted == ["hello"]


def test_hold_blocks_until_release(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "boss")
    d = make_dispatcher()
    d.hold("boss", owner="isaac", reason="typing on phone", ttl=None)
    item = d.enqueue("boss", "hello", source="t")
    run(d, 30)
    assert item.state is State.WAITING
    assert item.reason == "hold: typing on phone"
    d.release("boss")
    run(d, 2)
    assert pane.submitted == ["hello"]


def test_hold_expires(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    d.hold("w1:p1", owner="console", reason="", ttl=5)
    d.enqueue("w1:p1", "hello", source="t")
    run(d, 4)
    assert pane.submit_calls == 0
    run(d, 3)
    assert pane.submitted == ["hello"]


@pytest.mark.parametrize("status", ["blocked", "unknown"])
def test_unsendable_status_waits(herdr, make_dispatcher, run, status):
    pane = herdr.add("w1:p1", status=status)
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 10)
    assert item.state is State.WAITING
    assert item.reason == f"agent {status}"
    assert pane.submit_calls == 0


def test_working_agent_with_empty_box_gets_the_message(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", status="working")
    d = make_dispatcher()
    d.enqueue("w1:p1", "queued while working", source="t")
    run(d, 2)
    assert pane.submitted == ["queued while working"]


def test_dialog_reported_as_idle_still_waits(herdr, make_dispatcher, run):
    # herdr reported idle while Claude Code showed its trust dialog (HC1, test 2).
    pane = herdr.add("w1:p1", status="idle", dialog=True)
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 10)
    assert item.state is State.WAITING
    assert item.reason.startswith("input unknown")
    assert pane.submit_calls == 0


def test_identity_change_fails_instead_of_misdelivering(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="busy")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    pane.session = "s2"
    pane.box = ""
    run(d, 3)
    assert item.state is State.FAILED
    assert item.reason.startswith("identity_changed")
    assert pane.submit_calls == 0


def test_text_stuck_in_box_fails_once_and_is_not_resent(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", submit_mode="stuck")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "[lane] report", source="t")
    run(d, 15)
    assert item.state is State.FAILED
    assert item.reason.startswith("stuck_in_input")
    assert pane.submit_calls == 1


def test_rejected_submit_retries_with_count_then_fails(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", submit_mode="agent_blocked")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 200)
    assert item.state is State.FAILED
    assert pane.submit_calls == MAX_ATTEMPTS
    assert item.attempts == MAX_ATTEMPTS


def test_uncertain_submit_error_is_not_retried(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", submit_mode="cli_timeout")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 60)
    assert item.state is State.FAILED
    assert pane.submit_calls == 1


def test_fifo_per_pane_and_panes_are_independent(herdr, make_dispatcher, run):
    a = herdr.add("w1:pA", box="isaac is typing here")
    b = herdr.add("w1:pB")
    d = make_dispatcher()
    d.enqueue("w1:pA", "to A", source="t")
    d.enqueue("w1:pB", "first to B", source="t")
    d.enqueue("w1:pB", "second to B", source="t")
    for _ in range(3):
        run(d, 2)
        b.status = "idle"
    assert b.submitted == ["first to B", "second to B"]
    assert a.submit_calls == 0


def test_human_message_goes_ahead_of_agent_messages(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher()
    d.enqueue("w1:p1", "agent 1", source="lane")
    d.enqueue("w1:p1", "agent 2", source="lane")
    d.enqueue("w1:p1", "from isaac", source="console", human=True)
    pane.box = ""
    for _ in range(4):
        run(d, 2)
        pane.status = "idle"
    assert pane.submitted == ["from isaac", "agent 1", "agent 2"]


def test_target_gone_fails_after_retries(herdr, make_dispatcher, run):
    herdr.add("w1:p1", box="x")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    del herdr.panes["w1:p1"]
    run(d, 200)
    assert item.state is State.FAILED
    assert item.reason.startswith("target gone")


def test_unknown_target_is_rejected_at_enqueue(herdr, make_dispatcher):
    d = make_dispatcher()
    with pytest.raises(DispatchError, match="cannot resolve"):
        d.enqueue("nobody", "hello", source="t")


def test_cancel_queued_item(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    d.cancel(item.id)
    pane.box = ""
    run(d, 5)
    assert item.state is State.CANCELLED
    assert pane.submit_calls == 0


def test_restart_restores_pending_and_does_not_resend_unconfirmed(herdr, clock, tmp_path, run):
    journal = tmp_path / "journal.jsonl"
    a = herdr.add("w1:pA", box="typing")
    b = herdr.add("w1:pB", submit_mode="stuck")
    d = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    waiting = d.enqueue("w1:pA", "still queued", source="t")
    verifying = d.enqueue("w1:pB", "in flight", source="t")
    run(d, 2)
    assert verifying.state is State.VERIFYING

    restarted = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    assert restarted.items[verifying.id].state is State.FAILED
    assert restarted.items[waiting.id].state is State.QUEUED
    a.box = ""
    run(restarted, 2)
    assert a.submitted == ["still queued"]
    assert b.submit_calls == 1
    assert (tmp_path / "journal.prev.jsonl").exists()


def test_journal_records_each_transition(herdr, make_dispatcher, run, tmp_path):
    herdr.add("w1:p1")
    d = make_dispatcher()
    d.enqueue("w1:p1", "hello", source="t")
    run(d, 3)
    states = [
        json.loads(line)["state"]
        for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert states[0] == "queued"
    assert states[-2:] == ["verifying", "sent"]
