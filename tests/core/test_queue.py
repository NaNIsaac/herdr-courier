import json
import time

import pytest

from herdr_courier.core.queue import MAX_ATTEMPTS, Dispatcher, DispatchError, State


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


def test_unexpected_submit_exception_fails_without_resend(herdr, make_dispatcher, run):
    # A non-herdr exception after part of the message was written (seen when a transport
    # parsed empty output as JSON) must not send the item back to the queue.
    pane = herdr.add("w1:p1")
    calls = []

    def broken(pane_id, text):
        calls.append(text)
        pane.box = text
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    herdr.submit = broken
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "[lane] report", source="t")
    run(d, 20)
    assert item.state is State.FAILED
    assert calls == ["[lane] report"]


def test_restart_during_submit_never_writes_again(herdr, clock, tmp_path, run):
    # Review 1a: the process dies while herdr agent prompt is running. The journal must
    # already say SUBMITTING, so the next instance fails the item instead of resending it.
    journal = tmp_path / "journal.jsonl"
    pane = herdr.add("w1:p1")
    first = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    item = first.enqueue("w1:p1", "hello", source="t")
    restarted = {}
    real_submit = herdr.submit

    def submit_then_crash(pane_id, text):
        real_submit(pane_id, text)
        restarted["d"] = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
        raise SystemExit("killed mid-submit")

    herdr.submit = submit_then_crash
    with pytest.raises(SystemExit):
        for _ in range(10):
            first.tick()
            clock.now += 0.5
    second = restarted["d"]
    herdr.submit = real_submit
    pane.status = "idle"
    run(second, 10)
    assert pane.submitted == ["hello"]
    assert second.items[item.id].state is State.FAILED
    assert "submitting" in second.items[item.id].reason


def test_cancel_during_submit_is_refused(herdr, make_dispatcher, run):
    # Review 1b: a cancel that arrives while the text is being written must not be accepted,
    # or the message goes out while courier reports it cancelled.
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    refused = []
    real_submit = herdr.submit

    def submit_with_cancel(pane_id, text):
        try:
            d.cancel(item.id)
        except DispatchError as exc:
            refused.append(str(exc))
        real_submit(pane_id, text)

    herdr.submit = submit_with_cancel
    run(d, 3)
    assert refused and "submitting" in refused[0]
    assert item.state is State.SENT
    assert pane.submitted == ["hello"]
    assert d.wait(item.id, 0).state is State.SENT


def test_journal_failure_before_submit_stops_the_write(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    real_journal = d._journal

    def failing(entry):
        if entry.get("state") == "submitting":
            raise OSError("disk full")
        real_journal(entry)

    d._journal = failing
    run(d, 5)
    assert pane.submit_calls == 0
    assert item.state is State.WAITING
    assert "disk full" in item.reason


def test_holds_survive_a_restart(herdr, clock, tmp_path, run):
    journal = tmp_path / "journal.jsonl"
    pane = herdr.add("w1:p1")
    herdr.add("w1:p2")
    d = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    d.hold("w1:p1", owner="isaac", reason="typing on phone", ttl=None)
    d.hold("w1:p2", owner="console", reason="draft", ttl=60)
    d.hold("w1:p9", owner="x", reason="", ttl=None)
    d.release("w1:p9")
    d.enqueue("w1:p1", "after restart", source="t")

    restarted = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    assert set(restarted.holds) == {"w1:p1", "w1:p2"}
    assert restarted.holds["w1:p1"].reason == "typing on phone"
    assert restarted.holds["w1:p1"].restored
    assert 55 < restarted.holds["w1:p2"].until - clock.now <= 60
    run(restarted, 10)
    assert pane.submit_calls == 0
    status = restarted.status()["panes"]["w1:p1"]["hold"]
    assert status["restored"] is True
    restarted.release("w1:p1")
    run(restarted, 3)
    assert pane.submitted == ["after restart"]


def test_expired_hold_is_not_restored(herdr, clock, tmp_path, monkeypatch):
    journal = tmp_path / "journal.jsonl"
    herdr.add("w1:p1")
    d = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    d.hold("w1:p1", owner="t", reason="short", ttl=5)
    later = time.time() + 10
    monkeypatch.setattr("herdr_courier.core.queue.time.time", lambda: later)
    restarted = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    assert restarted.holds == {}


def _arm_after_first_pass(d, monkeypatch, action):
    """Run ``action`` once, right after the first observation that passes the gate."""
    import herdr_courier.core.queue as q

    real = q.check
    fired = []

    def spy(obs, **kw):
        verdict = real(obs, **kw)
        if verdict.ok and not fired:
            fired.append(True)
            action()
        return verdict

    monkeypatch.setattr(q, "check", spy)
    return fired


def test_final_check_sees_a_hold_placed_after_the_gate(herdr, make_dispatcher, run, monkeypatch):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    fired = _arm_after_first_pass(
        d, monkeypatch, lambda: d.hold("w1:p1", owner="isaac", reason="phone", ttl=None)
    )
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 5)
    assert fired
    assert pane.submit_calls == 0
    assert item.state is State.WAITING
    assert item.reason.startswith("hold: phone")


def test_final_check_sees_a_dialog_that_opened_after_the_gate(
    herdr, make_dispatcher, run, monkeypatch
):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()

    def block():
        pane.status = "blocked"

    _arm_after_first_pass(d, monkeypatch, block)
    item = d.enqueue("w1:p1", "hello", source="t")
    run(d, 5)
    assert pane.submit_calls == 0
    assert item.reason.startswith("agent blocked")


def test_lost_session_counts_as_identity_change(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "hello", source="t")
    pane.session = None
    run(d, 3)
    assert item.state is State.FAILED
    assert item.reason.startswith("identity_changed")
    assert pane.submit_calls == 0


def test_back_to_back_messages_each_wait_a_full_debounce(herdr, clock, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    times = []
    real_submit = herdr.submit

    def timed(pane_id, text):
        times.append(clock.now)
        real_submit(pane_id, text)
        pane.status = "idle"

    herdr.submit = timed
    d = make_dispatcher()
    d.enqueue("w1:p1", "first", source="t")
    d.enqueue("w1:p1", "second", source="t")
    run(d, 10)
    assert pane.submitted == ["first", "second"]
    assert times[1] - times[0] >= 1.0


def test_same_client_id_is_queued_once(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "boss")
    d = make_dispatcher()
    a = d.enqueue("boss", "hello", source="t", client_id="c" * 16)
    b = d.enqueue("boss", "hello", source="t", client_id="c" * 16)
    assert a is b
    run(d, 3)
    assert pane.submitted == ["hello"]
