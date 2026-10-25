"""A dispatcher serves one herdr server and fails closed when it cannot prove a pane is the one
a message was queued for.

Facts the fakes follow, read from herdr 0.9.5 on isolated named sessions: pane ids are numbered
per server (two servers both have w1:p1), a server restart keeps pane ids and gives every pane a
new terminal_id, a closed pane's id is not reused, and one server refuses a second agent of the
same name (agent_name_taken).
"""

import json
import os

import pytest

from herdr_courier.core.queue import MAX_ATTEMPTS, Dispatcher, DispatchError, State, WrongServer

A = os.path.join("herdr", "herdr.sock")
B = os.path.join("herdr", "sessions", "lab", "herdr.sock")


@pytest.fixture
def bound(herdr, clock, tmp_path):
    def make(server=A) -> Dispatcher:
        return Dispatcher(
            herdr, tmp_path / "journal.jsonl", clock=clock, log=lambda _: None, server=server
        )

    return make


def journal_text(tmp_path) -> str:
    return (tmp_path / "journal.jsonl").read_text(encoding="utf-8")


def test_queued_work_is_not_replayed_on_another_server(herdr, bound, tmp_path):
    herdr.add("w1:p1", "boss", session=None, terminal="term_a1")
    d = bound()
    d.hold("boss", owner="t", reason="typing", ttl=None)
    item = d.enqueue("boss", "for A's boss", source="t")
    before = journal_text(tmp_path)
    with pytest.raises(WrongServer, match="1 queued messages and 1 holds"):
        bound(B)
    assert journal_text(tmp_path) == before
    again = bound(A)
    assert again.items[item.id].state is State.QUEUED
    assert "w1:p1" in again.holds


def test_a_state_directory_with_nothing_left_may_serve_another_server(herdr, bound, run, tmp_path):
    pane = herdr.add("w1:p1", "boss")
    d = bound()
    d.enqueue("boss", "done before the move", source="t")
    run(d, 3)
    assert pane.submitted == ["done before the move"]
    bound(B)
    servers = [
        json.loads(line)["socket"]
        for line in journal_text(tmp_path).splitlines()
        if '"server"' in line
    ]
    assert servers[-1] == B


def test_one_server_spelled_two_ways_is_one_server(herdr, bound):
    herdr.add("w1:p1", "boss")
    bound(A).enqueue("boss", "hello", source="t")
    bound(os.path.join("herdr", ".", "herdr.sock"))


def test_a_journal_from_before_binding_is_adopted(herdr, make_dispatcher, bound):
    herdr.add("w1:p1", "boss")
    make_dispatcher().enqueue("boss", "queued by 0.2.0", source="t")
    d = bound(B)
    assert [i.state for i in d.items.values()] == [State.QUEUED]


def test_a_stopped_server_holds_messages_and_nothing_fails_over(herdr, bound, run):
    pane = herdr.add("w1:p1", "boss")
    d = bound()
    item = d.enqueue("boss", "hello", source="t")
    herdr.down = True
    run(d, 120)
    assert item.state is State.WAITING
    assert "server_not_running" in item.reason
    assert item.attempts == 0
    with pytest.raises(DispatchError, match="server_not_running"):
        d.enqueue("boss", "while down", source="t")
    herdr.down = False
    run(d, 3)
    assert pane.submitted == ["hello"]


def test_after_a_server_restart_a_session_proves_the_agent(herdr, bound, run):
    pane = herdr.add("w1:p1", "boss", session="s1", terminal="term_a1")
    d = bound()
    d.hold("boss", owner="t", reason="", ttl=None)
    item = d.enqueue("boss", "hello", source="t")
    # The restored agent resumed its session in a new terminal.
    pane.terminal = "term_b7"
    d.release("boss")
    run(d, 3)
    assert item.state is State.SENT
    assert pane.submitted == ["hello"]


def test_after_a_server_restart_a_message_without_session_is_not_sent(herdr, bound, run):
    pane = herdr.add("w1:p1", "boss", session=None, terminal="term_a1")
    d = bound()
    d.hold("boss", owner="t", reason="", ttl=None)
    item = d.enqueue("boss", "hello", source="t")
    pane.terminal = "term_b7"
    d.release("boss")
    run(d, 3)
    assert item.state is State.FAILED
    assert item.reason == "identity_changed: terminal term_b7 is not term_a1"
    assert pane.submit_calls == 0


def test_a_restarted_pane_does_not_inherit_the_old_panes_messages(herdr, bound, run):
    old = herdr.add("w1:p1", "boss", session=None, terminal="term_a1")
    d = bound()
    d.hold("boss", owner="t", reason="", ttl=None)
    item = d.enqueue("boss", "hello", source="t")
    del herdr.panes["w1:p1"]
    new = herdr.add("w1:p2", "boss", session=None, terminal="term_a2")
    d.release("w1:p1")
    run(d, 120)
    assert item.state is State.FAILED
    assert item.reason.startswith("target gone")
    assert item.attempts == MAX_ATTEMPTS
    assert old.submit_calls == new.submit_calls == 0


def test_a_name_moved_while_queued_does_not_redirect_the_message(herdr, bound, run):
    first = herdr.add("w1:p1", "boss", session="s1", terminal="term_a1")
    d = bound()
    d.hold("boss", owner="t", reason="", ttl=None)
    item = d.enqueue("boss", "for the first boss", source="t")
    # boss is now another pane; w1:p1 runs another agent session.
    second = herdr.add("w1:p2", "boss", session="s9", terminal="term_a2")
    first.session = "s2"
    d.release("w1:p1")
    run(d, 3)
    assert item.state is State.FAILED
    assert item.reason == "identity_changed: session s2 is not s1"
    assert first.submit_calls == second.submit_calls == 0
