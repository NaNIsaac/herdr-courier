import json
import threading

import pytest

from herdr_courier import cli
from herdr_courier.core.queue import Dispatcher, DispatchError, State, WrongServer


def test_submit_is_worker_owned_and_exact_without_pasting(herdr, make_dispatcher, run, monkeypatch):
    # About the first Enter retry; later Enters on a ghost are tested in test_ghost_enter.py.
    monkeypatch.setattr("herdr_courier.core.queue.GHOST_ENTERS", 0)
    pane = herdr.add("w1", submit_mode="stuck", terminal="term")
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1", "exact courier body", source="test")
    run(dispatcher, 15)
    assert item.state is State.GHOST
    keys = []

    def enter(target):
        keys.append(threading.get_ident())
        pane.box = ""
        pane.status = "working"

    monkeypatch.setattr(herdr, "enter", enter)
    future = dispatcher.request_submit("w1")
    assert future is dispatcher.request_submit("w1")
    assert not keys
    dispatcher.tick()
    assert future.result() is item
    assert keys == [threading.get_ident()]
    assert pane.submit_calls == 1
    assert item.state is State.SENT


@pytest.mark.parametrize(
    "change", ["suffix", "prefix", "focused", "hold", "session", "terminal", "working", "unknown"]
)
def test_submit_refuses_human_edits_and_guards(herdr, make_dispatcher, change):
    pane = herdr.add("w1", terminal="term")
    dispatcher = make_dispatcher()
    dispatcher.enqueue("w1", "known text", source="test")
    pane.box = "known text"
    if change in ("suffix", "prefix"):
        pane.box = pane.box + " human" if change == "suffix" else "human " + pane.box
    elif change == "focused":
        pane.focused = True
    elif change == "hold":
        dispatcher.hold("w1", owner="test", reason="typing", ttl=None)
    elif change in ("session", "terminal"):
        setattr(pane, change, "another")
    elif change == "working":
        pane.status = "working"
    else:
        pane.dialog = True
    future = dispatcher.request_submit("w1")
    dispatcher.tick()
    with pytest.raises(DispatchError):
        future.result()
    assert not pane.enter_calls
    assert not pane.submit_calls


def test_submit_rechecks_edit_after_ownership_match(herdr, make_dispatcher, monkeypatch):
    pane = herdr.add("w1", box="known text", terminal="term")
    dispatcher = make_dispatcher()
    dispatcher.enqueue("w1", "known text", source="test")
    original = herdr.read_ansi
    reads = []

    def read(*args, **kwargs):
        reads.append(1)
        if len(reads) == 2:
            pane.box += " edited"
        return original(*args, **kwargs)

    monkeypatch.setattr(herdr, "read_ansi", read)
    future = dispatcher.request_submit("w1")
    dispatcher.tick()
    with pytest.raises(DispatchError, match="no longer"):
        future.result()
    assert not pane.enter_calls


def test_submit_intent_is_journalled_before_key_and_restart_never_replays(
    herdr, clock, tmp_path, run, monkeypatch
):
    pane = herdr.add("w1", submit_mode="stuck", terminal="term")
    dispatcher = Dispatcher(herdr, tmp_path / "journal", clock=clock, log=lambda _: None)
    item = dispatcher.enqueue("w1", "known text", source="test")
    run(dispatcher, 15)
    keys = []

    def crash(target):
        entries = [json.loads(row) for row in (tmp_path / "journal").read_text().splitlines()]
        assert entries[-1]["enter_retried"] is True
        keys.append(target)
        raise SystemExit("crash")

    monkeypatch.setattr(herdr, "enter", crash)
    dispatcher.request_submit("w1")
    with pytest.raises(SystemExit):
        dispatcher.tick()
    restored = Dispatcher(herdr, tmp_path / "journal", clock=clock, log=lambda _: None)
    assert restored.items[item.id].state is State.FAILED
    run(restored, 15)
    assert keys == ["w1"]
    assert pane.submit_calls == 1


def test_submit_cli_calls_daemon_not_raw_herdr(monkeypatch, capsys):
    calls = []

    def call(method, path, body, **kwargs):
        calls.append((method, path, body))
        return {"item": {"id": "123", "state": "verifying", "reason": "confirming"}}

    monkeypatch.setattr(cli, "call", call)
    assert cli.main(["submit", "receiver"]) == 0
    assert calls[0][0:2] == ("POST", "submit")
    assert calls[0][2]["target"] == "receiver"
    assert "verifying" in capsys.readouterr().out


def test_failed_final_journal_write_keeps_item_in_its_queue(
    herdr, make_dispatcher, run, monkeypatch
):
    herdr.add("w1", submit_mode="stuck", terminal="term")
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1", "owned text", source="test")
    run(dispatcher, 2)
    assert item.state is State.VERIFYING

    def full(entry):
        raise OSError("disk full")

    monkeypatch.setattr(dispatcher, "_journal", full)
    with dispatcher.lock, pytest.raises(OSError):
        dispatcher._finish(item, State.GHOST, "ghost")
    assert item.state is State.VERIFYING
    assert item.id in dispatcher.queues["w1"]


def test_recoverable_ghost_cannot_cross_herdr_servers(herdr, clock, tmp_path, run, monkeypatch):
    # About the first Enter retry; later Enters on a ghost are tested in test_ghost_enter.py.
    monkeypatch.setattr("herdr_courier.core.queue.GHOST_ENTERS", 0)
    pane = herdr.add("w1", submit_mode="stuck", terminal="term")
    dispatcher = Dispatcher(
        herdr, tmp_path / "journal", clock=clock, server="socket-a", log=lambda _: None
    )
    item = dispatcher.enqueue("w1", "owned body", source="test")
    run(dispatcher, 15)
    assert item.state is State.GHOST
    before = pane.enter_calls
    with pytest.raises(WrongServer):
        Dispatcher(herdr, tmp_path / "journal", clock=clock, server="socket-b", log=lambda _: None)
    assert pane.enter_calls == before
