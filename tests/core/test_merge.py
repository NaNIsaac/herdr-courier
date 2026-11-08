import pytest

from herdr_courier.core.queue import Dispatcher, DispatchError, State, _framed


def merged(*items):
    return "\n\n".join(_framed(i) for i in items)


def test_messages_waiting_for_a_ready_pane_go_in_one_prompt(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "boss", box="typing")
    d = make_dispatcher()
    a = d.enqueue("boss", "first", source="lane-a")
    b = d.enqueue("boss", "second\nwith two lines", source="lane-b")
    c = d.enqueue("boss", "third", source="lane-a")
    run(d, 2)
    pane.box = ""
    run(d, 3)
    assert pane.submit_calls == 1
    assert pane.submitted == [merged(a, b, c)]
    assert pane.submitted[0].startswith(f"--- {a.id} from lane-a at ")
    assert [i.state for i in (a, b, c)] == [State.SENT] * 3
    assert b.reason.startswith(f"with {a.id}: ")
    assert not d.queues["w1:p1"]


def test_one_message_goes_without_a_header(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    d.enqueue("w1:p1", "alone", source="t")
    run(d, 3)
    assert pane.submitted == ["alone"]


def test_human_messages_go_first_and_merge_only_with_each_other(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher()
    a1 = d.enqueue("w1:p1", "agent 1", source="lane")
    a2 = d.enqueue("w1:p1", "agent 2", source="lane")
    h1 = d.enqueue("w1:p1", "from owner", source="console", human=True)
    h2 = d.enqueue("w1:p1", "and again", source="console", human=True)
    pane.box = ""
    for _ in range(4):
        run(d, 2)
        pane.status = "idle"
    assert pane.submitted == [merged(h1, h2), merged(a1, a2)]


@pytest.mark.parametrize("command", ["/compact", "  /clear now"])
def test_slash_commands_always_go_alone(herdr, make_dispatcher, run, command):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher()
    a = d.enqueue("w1:p1", "before", source="t")
    d.enqueue("w1:p1", command, source="t")
    b = d.enqueue("w1:p1", "after 1", source="t")
    c = d.enqueue("w1:p1", "after 2", source="t")
    pane.box = ""
    for _ in range(4):
        run(d, 2)
        pane.status = "idle"
    assert pane.submitted == ["before", command, merged(b, c)]
    assert a.state is State.SENT


def test_merged_prompt_stays_within_the_limit_and_the_rest_go_next(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher(merge_chars=200)
    items = [d.enqueue("w1:p1", f"message {n} " + "." * 40, source="t") for n in range(4)]
    assert len(merged(*items[:2])) <= 200 < len(merged(*items[:3]))
    pane.box = ""
    for _ in range(4):
        run(d, 2)
        pane.status = "idle"
    assert pane.submitted == [merged(*items[:2]), merged(*items[2:])]


def test_a_message_longer_than_the_limit_still_goes_alone(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher(merge_chars=20)
    d.enqueue("w1:p1", "a long first message", source="t")
    d.enqueue("w1:p1", "next", source="t")
    pane.box = ""
    for _ in range(3):
        run(d, 2)
        pane.status = "idle"
    assert pane.submitted == ["a long first message", "next"]


def test_zero_turns_merging_off(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher(merge_chars=0)
    d.enqueue("w1:p1", "one", source="t")
    d.enqueue("w1:p1", "two", source="t")
    pane.box = ""
    for _ in range(3):
        run(d, 2)
        pane.status = "idle"
    assert pane.submitted == ["one", "two"]


def test_a_message_bound_to_another_session_does_not_ride_along(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", box="x")
    d = make_dispatcher()
    a = d.enqueue("w1:p1", "old session", source="t")
    pane.session = "s2"
    b = d.enqueue("w1:p1", "new session", source="t")
    pane.session = "s1"
    pane.box = ""
    run(d, 3)
    assert pane.submitted == ["old session"]
    assert a.state is State.SENT
    assert b.state is not State.SENT


def test_riders_are_in_flight_with_the_first_and_cannot_be_cancelled(herdr, make_dispatcher, run):
    herdr.add("w1:p1", submit_mode="stuck")
    d = make_dispatcher()
    a = d.enqueue("w1:p1", "first", source="t")
    b = d.enqueue("w1:p1", "second", source="t")
    run(d, 2)
    assert a.state is State.VERIFYING
    assert b.state is State.SUBMITTING
    with pytest.raises(DispatchError, match="too late"):
        d.cancel(b.id)


def test_restart_during_a_merged_submit_fails_every_part(herdr, clock, tmp_path, run):
    journal = tmp_path / "journal.jsonl"
    pane = herdr.add("w1:p1")
    first = Dispatcher(herdr, journal, clock=clock, log=lambda _: None)
    a = first.enqueue("w1:p1", "first", source="t")
    b = first.enqueue("w1:p1", "second", source="t")
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
    assert pane.submit_calls == 1
    assert second.items[a.id].state is State.FAILED
    assert second.items[b.id].state is State.FAILED


def test_rejected_submit_puts_riders_back_in_line(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", submit_mode="agent_not_ready")
    d = make_dispatcher()
    a = d.enqueue("w1:p1", "first", source="t")
    b = d.enqueue("w1:p1", "second", source="t")
    run(d, 2)
    assert pane.submit_calls == 1
    assert a.state is State.WAITING
    assert b.state is State.WAITING
    assert b.attempts == 0
    pane.submit_mode = "ok"
    run(d, 10)
    assert pane.submitted == [merged(a, b)]
    assert b.state is State.SENT


def test_failed_submit_fails_the_riders_too(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", submit_mode="io_error")
    d = make_dispatcher()
    a = d.enqueue("w1:p1", "first", source="t")
    b = d.enqueue("w1:p1", "second", source="t")
    run(d, 5)
    assert pane.submit_calls == 1
    assert a.state is State.FAILED
    assert b.state is State.FAILED
    assert b.reason.startswith(f"with {a.id}: submit failed")


def test_merged_ghost_can_be_submitted_by_hand_after_a_restart(
    herdr, clock, tmp_path, run, monkeypatch
):
    journal = tmp_path / "journal.jsonl"
    pane = herdr.add("w1:p1", submit_mode="stuck", terminal="term")
    first = Dispatcher(herdr, journal, clock=clock, wall=clock, log=lambda _: None)
    a = first.enqueue("w1:p1", "first", source="t")
    b = first.enqueue("w1:p1", "second", source="t")
    run(first, 15)
    assert (a.state, b.state) == (State.GHOST, State.GHOST)
    assert pane.box == merged(a, b)

    second = Dispatcher(herdr, journal, clock=clock, wall=clock, log=lambda _: None)

    def enter(target):
        pane.box = ""
        pane.status = "working"

    monkeypatch.setattr(herdr, "enter", enter)
    future = second.request_submit("w1:p1")
    second.tick()
    assert future.result().id == a.id
    run(second, 6)
    assert second.items[a.id].state is State.SENT
    assert second.items[b.id].state is State.SENT
    assert pane.submit_calls == 1
