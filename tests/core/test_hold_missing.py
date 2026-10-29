import pytest

from herdr_courier.core.queue import (
    HOLD_TTL,
    RELEASE_AT_ONCE,
    SUMMARY_EVERY,
    Dispatcher,
    DispatchError,
    State,
)


@pytest.fixture
def failover(herdr, clock, tmp_path):
    def make() -> Dispatcher:
        return Dispatcher(
            herdr,
            tmp_path / "journal.jsonl",
            clock=clock,
            wall=clock,
            log=lambda _: None,
            hold_missing=("boss", "secretary"),
            summary_to={"boss": "secretary", "secretary": "boss"},
        )

    return make


def boss_gone_after_one_message(herdr, d, run):
    """boss had session s1 when courier last saw it, then left herdr."""
    herdr.add("w1:p1", "boss")
    d.enqueue("boss", "before", source="lane")
    run(d, 3)
    del herdr.panes["w1:p1"]
    del herdr.names["boss"]


def boss_back(herdr, pane_id="w1:p9", session="s1"):
    pane = herdr.add(pane_id, "boss", session=session)
    return pane


def test_a_message_for_a_missing_boss_is_held_and_secretary_gets_one_summary(herdr, failover, run):
    sec = herdr.add("w1:p2", "secretary")
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    first = d.enqueue("boss", "report 1", source="design")
    second = d.enqueue("boss", "report 2", source="icc")
    run(d, 10)
    assert first.state is second.state is State.WAITING
    assert first.reason.startswith("hold: boss missing since ")
    assert len(sec.submitted) == 1
    assert sec.submitted[0].startswith("courier: boss missing since ")
    assert "held 2 (design 1, icc 1)" in sec.submitted[0]
    assert d.status()["held"]["boss"]["pending"][0]["text"] == "report 1"


def test_an_unknown_agent_outside_the_list_is_still_refused(herdr, failover):
    d = failover()
    with pytest.raises(DispatchError, match="cannot resolve"):
        d.enqueue("design", "hello", source="t")


def test_summaries_come_at_most_every_15_minutes_and_only_on_news(herdr, failover, run, clock):
    sec = herdr.add("w1:p2", "secretary")
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    d.enqueue("boss", "one", source="lane")
    run(d, 10)
    d.enqueue("boss", "two", source="lane")
    run(d, 60)
    assert len(sec.submitted) == 1
    sec.status = "idle"
    clock.now += SUMMARY_EVERY
    run(d, 10)
    assert len(sec.submitted) == 2
    assert "held 2" in sec.submitted[1]
    sec.status = "idle"
    clock.now += SUMMARY_EVERY
    run(d, 10)
    assert len(sec.submitted) == 2


def test_boss_back_with_the_same_session_gets_the_held_messages_oldest_first(herdr, failover, run):
    sec = herdr.add("w1:p2", "secretary")
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    texts = [f"report {n}" for n in range(RELEASE_AT_ONCE + 2)]
    for text in texts:
        d.enqueue("boss", text, source="lane")
    run(d, 3)
    back = boss_back(herdr)
    for _ in range(RELEASE_AT_ONCE + 3):
        run(d, 3)
        back.status = "idle"
    assert back.submitted == [
        *texts[:RELEASE_AT_ONCE],
        "courier: 2 more held for you: `courier inbox`",
    ]
    assert d.status()["held"]["boss"]["pending"][0]["text"] == texts[RELEASE_AT_ONCE]
    sec.status = "idle"
    run(d, 3)
    assert "back with the same session; 10 typed in" in sec.submitted[-1]


def test_boss_back_with_a_new_session_gets_nothing_typed_in(herdr, failover, run):
    sec = herdr.add("w1:p2", "secretary")
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    item = d.enqueue("boss", "report", source="lane")
    run(d, 3)
    sec.status = "idle"
    back = boss_back(herdr, session="s7")
    run(d, 10)
    assert back.submit_calls == 0
    assert item.state is State.WAITING
    assert "back with a new session; the held stay held: `courier inbox boss`" in sec.submitted[-1]
    # New messages go to the new session as usual.
    d.enqueue("boss", "fresh", source="lane")
    run(d, 3)
    assert back.submitted == ["fresh"]


def test_a_held_message_fails_after_the_ttl_and_the_summary_says_so(herdr, failover, run, clock):
    sec = herdr.add("w1:p2", "secretary")
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    item = d.enqueue("boss", "report", source="lane")
    run(d, 3)
    sec.status = "idle"
    clock.now += HOLD_TTL
    run(d, 10)
    assert item.state is State.FAILED
    assert item.reason.startswith("hold expired: hold: boss missing since ")
    assert "1 failed after 4 h held" in sec.submitted[-1]


def test_both_missing_sends_no_summary_and_nothing_loops(herdr, failover, run):
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    to_boss = d.enqueue("boss", "for boss", source="lane")
    to_sec = d.enqueue("secretary", "for secretary", source="lane")
    run(d, 120)
    assert to_boss.state is to_sec.state is State.WAITING
    assert [i.text for i in d.items.values() if i.source == "courier"] == []


def test_a_queued_message_follows_boss_to_its_new_pane_after_a_resume(herdr, failover, run):
    old = herdr.add("w1:p1", "boss", box="busy")
    d = failover()
    item = d.enqueue("boss", "report", source="lane")
    run(d, 3)
    del herdr.panes["w1:p1"]
    del herdr.names["boss"]
    run(d, 3)
    assert item.state is State.WAITING
    assert item.reason.startswith("hold: boss missing")
    new = boss_back(herdr, "w1:p5")
    run(d, 10)
    assert item.state is State.SENT
    assert new.submitted == ["report"]
    assert old.submit_calls == 0


def test_a_message_for_boss_whose_pane_lost_its_session_waits_for_the_resume(herdr, failover, run):
    pane = herdr.add("w1:p1", "boss", box="busy")
    d = failover()
    item = d.enqueue("boss", "report", source="lane")
    pane.session = None
    pane.box = ""
    run(d, 10)
    assert item.state is State.WAITING
    assert pane.submit_calls == 0
    pane.session = "s1"
    run(d, 10)
    assert pane.submitted == ["report"]


def test_held_messages_survive_a_restart(herdr, failover, run):
    d = failover()
    boss_gone_after_one_message(herdr, d, run)
    item = d.enqueue("boss", "report", source="lane")
    run(d, 3)
    d2 = failover()
    assert d2.status()["held"]["boss"]["pending"][0]["id"] == item.id
    back = boss_back(herdr)
    run(d2, 10)
    assert back.submitted == ["report"]
    assert d2.items[item.id].state is State.SENT
