import pytest

from herdr_courier import routes
from herdr_courier.core.queue import ROUTE_MISSED, Dispatcher, State


@pytest.fixture
def routed(herdr, clock, tmp_path):
    def make(**kw) -> Dispatcher:
        return Dispatcher(
            herdr,
            tmp_path / "journal.jsonl",
            clock=clock,
            wall=clock,
            log=lambda _: None,
            routes_in=tmp_path,
            **kw,
        )

    return make


def test_a_message_for_a_routed_name_goes_to_the_deputy(herdr, routed, run, tmp_path):
    boss = herdr.add("w1:p1", "boss")
    deputy = herdr.add("w1:p2", "boss-deputy", session="d1")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    item = d.enqueue("boss", "report", source="lane")
    assert (item.target, item.pane_id, item.routed_from) == ("boss-deputy", "w1:p2", "boss")
    run(d, 3)
    assert item.state is State.SENT
    assert deputy.submitted == ["report"]
    assert boss.submitted == []


def test_clearing_the_route_sends_to_the_name_again(herdr, routed, run, tmp_path):
    boss = herdr.add("w1:p1", "boss")
    deputy = herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    assert d.enqueue("boss", "one", source="lane").pane_id == "w1:p2"
    routes.save(tmp_path, {})
    item = d.enqueue("boss", "two", source="lane")
    assert (item.pane_id, item.routed_from) == ("w1:p1", None)
    run(d, 3)
    # "one" had not gone out yet, so it follows the route off too, in the order it was sent.
    assert boss.submitted == ["one", "two"]
    assert deputy.submitted == []


def test_a_missing_deputy_leaves_the_message_with_its_name_and_says_so(
    herdr, routed, run, tmp_path
):
    boss = herdr.add("w1:p1", "boss")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    item = d.enqueue("boss", "report", source="lane")
    assert (item.target, item.routed_from) == ("boss", None)
    assert item.reason.startswith(ROUTE_MISSED)
    run(d, 3)
    assert boss.submitted == ["report"]


def test_a_deputy_pane_without_an_agent_session_is_not_a_route_target(herdr, routed, tmp_path):
    herdr.add("w1:p1", "boss")
    herdr.add("w1:p2", "boss-deputy", session="")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    assert routed().enqueue("boss", "report", source="lane").pane_id == "w1:p1"


def test_a_missing_deputy_and_a_missing_boss_hold_the_message_for_boss(herdr, routed, tmp_path):
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed(hold_missing=("boss",))
    item = d.enqueue("boss", "report", source="lane")
    assert (item.state, item.target, item.pane_id) == (State.WAITING, "boss", "")
    assert item.reason.startswith(ROUTE_MISSED)
    assert d.status()["held"]["boss"]["pending"][0]["id"] == item.id


def test_routes_that_loop_are_refused():
    with pytest.raises(routes.RouteError, match="loop: b → a → b"):
        routes.with_route({"a": "b"}, "b", "a")
    with pytest.raises(routes.RouteError, match="itself"):
        routes.with_route({}, "boss", "boss")
    with pytest.raises(routes.RouteError, match="pane id"):
        routes.with_route({}, "boss", "w1:p2")
    assert routes.with_route({"a": "b"}, "b", "c") == {"a": "b", "b": "c"}


def test_a_looping_file_written_by_hand_routes_nothing(herdr, routed, tmp_path):
    herdr.add("w1:p1", "boss")
    herdr.add("w1:p2", "boss-deputy")
    routes.path(tmp_path).write_text('{"boss": "boss-deputy", "boss-deputy": "boss"}')
    assert routed().enqueue("boss", "report", source="lane").pane_id == "w1:p1"


def test_a_pane_id_target_is_never_routed(herdr, routed, tmp_path):
    herdr.add("w1:p1", "boss")
    herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    assert routed().enqueue("w1:p1", "report", source="lane").pane_id == "w1:p1"


def test_turning_a_route_on_moves_queued_messages_but_not_one_in_flight(
    herdr, routed, run, tmp_path
):
    boss = herdr.add("w1:p1", "boss")
    deputy = herdr.add("w1:p2", "boss-deputy", session="d1")
    d = routed()
    d.hold("boss", owner="test", reason="out of limit", ttl=None)
    first = d.enqueue("boss", "one", source="lane")
    d.enqueue("boss", "two", source="icc")
    run(d, 2)
    assert boss.submitted == []
    routes.save(tmp_path, {"boss": "boss-deputy"})
    run(d, 3)
    assert deputy.submitted == ["one", "two"]
    assert (first.target, first.routed_from, first.session) == ("boss-deputy", "boss", "d1")
    assert boss.submitted == []

    d.release("boss")
    d.hold("boss-deputy", owner="test", reason="busy", ttl=None)
    third = d.enqueue("boss", "three", source="lane")
    assert third.pane_id == "w1:p2"
    routes.save(tmp_path, {})
    run(d, 3)
    assert (third.target, third.routed_from, third.state) == ("boss", None, State.SENT)
    assert boss.submitted == ["three"]


def test_held_messages_for_a_missing_boss_go_to_the_deputy_when_routed(
    herdr, routed, run, tmp_path
):
    d = routed(hold_missing=("boss",))
    item = d.enqueue("boss", "report", source="lane")
    assert item.state is State.WAITING
    deputy = herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    run(d, 3)
    assert deputy.submitted == ["report"]
    assert "boss" not in d.status()["held"]


def test_a_closed_deputy_pane_sends_its_queue_back_to_boss(herdr, routed, run, tmp_path):
    boss = herdr.add("w1:p1", "boss")
    herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    d.hold("boss-deputy", owner="test", reason="busy", ttl=None)
    item = d.enqueue("boss", "report", source="lane")
    del herdr.panes["w1:p2"]
    del herdr.names["boss-deputy"]
    run(d, 3)
    assert (item.state, item.target, item.routed_from) == (State.SENT, "boss", None)
    assert boss.submitted == ["report"]


def test_a_closed_deputy_with_boss_missing_holds_for_boss(herdr, routed, run, tmp_path):
    herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed(hold_missing=("boss",))
    d.hold("boss-deputy", owner="test", reason="busy", ttl=None)
    item = d.enqueue("boss", "report", source="lane")
    del herdr.panes["w1:p2"]
    del herdr.names["boss-deputy"]
    run(d, 3)
    assert (item.state, item.target, item.pane_id) == (State.WAITING, "boss", "")
    assert d.status()["held"]["boss"]["pending"][0]["id"] == item.id


def test_a_new_agent_in_the_deputy_pane_sends_the_queue_back(herdr, routed, run, tmp_path):
    boss = herdr.add("w1:p1", "boss")
    deputy = herdr.add("w1:p2", "boss-deputy", session="d1")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    d.hold("boss-deputy", owner="test", reason="busy", ttl=None)
    item = d.enqueue("boss", "report", source="lane")
    deputy.session = "d2"
    d.release("boss-deputy")
    run(d, 3)
    assert boss.submitted == ["report"]
    assert deputy.submitted == []
    assert item.state is State.SENT


def test_a_routed_message_is_still_routed_after_a_restart(herdr, routed, run, tmp_path):
    herdr.add("w1:p1", "boss")
    deputy = herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    d.hold("boss-deputy", owner="test", reason="busy", ttl=1.0)
    item = d.enqueue("boss", "report", source="lane")
    again = routed()
    restored = again.items[item.id]
    assert (restored.target, restored.pane_id, restored.routed_from) == (
        "boss-deputy",
        "w1:p2",
        "boss",
    )
    run(again, 4)
    assert deputy.submitted == ["report"]


def test_status_names_the_routes(herdr, routed, tmp_path):
    herdr.add("w1:p2", "boss-deputy")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    d = routed()
    d.tick()
    assert d.status()["routes"] == {"boss": "boss-deputy"}


def test_the_deputy_acts_as_controller_while_routed(herdr, routed, tmp_path):
    herdr.add("w1:p1", "boss")
    herdr.add("w1:p2", "boss-deputy")
    herdr.add("w1:p3", "lane")
    d = routed()
    task = {"key": "BL-1", "evidence": ["file missing.txt"]}
    d.send_task("lane", "do it", source="w1:p1", task=task)
    with pytest.raises(Exception, match="controller"):
        d.cancel_task("BL-1", sender_pane="w1:p2")
    routes.save(tmp_path, {"boss": "boss-deputy"})
    assert d.cancel_task("BL-1", sender_pane="w1:p2")["state"] == "CANCELLED"
