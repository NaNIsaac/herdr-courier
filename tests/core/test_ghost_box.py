import pytest

from herdr_courier.core.queue import Dispatcher, DispatchError, State


@pytest.mark.parametrize(
    "text", ["see the folder logs\\run1\\", "cut by quoting: \\", "end \\  \n"]
)
def test_a_message_ending_in_a_backslash_is_refused(herdr, make_dispatcher, text):
    pane = herdr.add("w1:p1", "secretary")
    d = make_dispatcher()
    with pytest.raises(DispatchError, match="ends with a backslash"):
        d.enqueue("secretary", text, source="t")
    assert d.items == {}
    assert pane.submit_calls == 0


def test_a_backslash_inside_a_message_still_goes(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    d.enqueue("w1:p1", "path logs\\run1\\out is ready", source="t")
    run(d, 3)
    assert pane.submitted == ["path logs\\run1\\out is ready"]


def left_changed(pane):
    """The Enter is taken as a new line and the box no longer equals the courier text exactly,
    so courier may neither press Enter again nor call it sent."""

    def submit(pane_id, text):
        pane.submit_calls += 1
        pane.box += text + "\n..."

    return submit


def test_text_left_in_the_box_is_reported_once_to_someone_else(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1", "lane")
    sec = herdr.add("w1:p2", "secretary", box="busy")
    herdr.submit = left_changed(pane)
    d = make_dispatcher()
    item = d.enqueue("lane", "report", source="t")
    run(d, 20)
    assert item.state is State.GHOST
    assert item.reason == "ghost: edited text remains in the input box"
    alerts = [i for i in d.items.values() if i.target == "secretary"]
    assert len(alerts) == 1
    assert alerts[0].text.startswith(f"courier: {item.id} for lane (w1:p1) was left unsent")
    assert "Clear that box before anyone types there" in alerts[0].text
    run(d, 20)
    assert len([i for i in d.items.values() if i.target == "secretary"]) == 1
    assert sec.submit_calls == 0


def test_a_ghost_in_the_notice_receivers_box_goes_to_the_controller(herdr, make_dispatcher, run):
    sec = herdr.add("w1:p1", "secretary", submit_mode="stuck")
    herdr.add("w1:p2", "boss", box="busy")
    d = make_dispatcher()
    item = d.enqueue("secretary", "report", source="t")
    run(d, 20)
    assert item.state is State.GHOST
    assert sec.box == "report"
    [alert] = [i for i in d.items.values() if i.target == "boss"]
    assert item.id in alert.text
    assert not [i for i in d.items.values() if i.target == "secretary" and i is not item]


def test_a_merged_ghost_is_reported_once_for_all_its_parts(herdr, make_dispatcher, run):
    herdr.add("w1:p1", "lane", submit_mode="stuck")
    herdr.add("w1:p2", "secretary", box="busy")
    d = make_dispatcher()
    a = d.enqueue("lane", "first", source="t")
    b = d.enqueue("lane", "second", source="t")
    run(d, 20)
    assert (a.state, b.state) == (State.GHOST, State.GHOST)
    [alert] = [i for i in d.items.values() if i.target == "secretary"]
    assert a.id in alert.text


def test_a_ghost_restored_after_a_restart_is_not_reported_again(herdr, clock, tmp_path, run):
    journal = tmp_path / "journal.jsonl"
    herdr.add("w1:p1", "lane", submit_mode="stuck")
    herdr.add("w1:p2", "secretary", box="busy")
    first = Dispatcher(herdr, journal, clock=clock, wall=clock, log=lambda _: None)
    item = first.enqueue("lane", "report", source="t")
    run(first, 20)
    assert item.state is State.GHOST
    second = Dispatcher(herdr, journal, clock=clock, wall=clock, log=lambda _: None)
    run(second, 20)
    assert second.items[item.id].state is State.GHOST
    # Only the alert the first run queued, restored with the journal.
    assert len([i for i in second.items.values() if i.target == "secretary"]) == 1


def test_with_no_one_else_to_tell_the_ghost_is_logged(herdr, clock, tmp_path, run):
    herdr.add("w1:p1", "solo", submit_mode="stuck")
    lines = []
    d = Dispatcher(
        herdr,
        tmp_path / "journal.jsonl",
        clock=clock,
        wall=clock,
        log=lines.append,
        controller="solo",
        secretary="solo",
    )
    item = d.enqueue("solo", "report", source="t")
    run(d, 20)
    assert item.state is State.GHOST
    assert any(line.startswith(f"courier: {item.id} for solo") for line in lines)
    assert len(d.items) == 1
