from pathlib import Path

import pytest

from herdr_courier import mailbox
from herdr_courier.core.queue import USAGE_PROBE_SECONDS, State, pointer
from herdr_courier.core.usage_stop import PREFIXES, usage_stop

STOP = "You're out of usage credits. /model to switch models."
STOPS = (*PREFIXES, "usage limit reached; send a message after it resets")


def stopped(screen: str) -> str:
    """``screen`` with the stop reply under its last output, above the input box."""
    return screen.replace("● earlier output", f"● earlier output\r\n  ⎿  {STOP}", 1)


NON_STOPS = (
    "You've hit your fast limit",
    "Fable limit reached · continuing on usage credits",
    "usage limit reached · continues automatically when it resets",
    "Usage limit reached · continuing automatically",
    "Usage limit reached · wrapping up",
    "earlier: You're out of usage credits",
    "you're out of usage credits",
    "send a message after it resets; usage limit reached",
    "",
)


@pytest.mark.parametrize("line", STOPS)
@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_stop_lines(line, apostrophe):
    line = line.replace("'", apostrophe)
    assert usage_stop(f"│ ● {line}\n\n❯\n") == line[:120]


@pytest.mark.parametrize("line", NON_STOPS)
@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_non_stop_lines(line, apostrophe):
    assert usage_stop(line.replace("'", apostrophe)) is None


def test_tail_and_truncation():
    assert usage_stop(STOP + "\n" + "ok\n" * 15) is None
    assert usage_stop(STOP + "\n\n" + "ok\n" * 14) == STOP
    assert usage_stop(STOP + "x" * 150) == (STOP + "x" * 150)[:120]


@pytest.mark.parametrize("reader", [False, True])
@pytest.mark.parametrize("after_submit", [False, True])
def test_usage_stop_holds_and_recovers(
    herdr, clock, tmp_path, make_dispatcher, run, monkeypatch, reader, after_submit
):
    pane = herdr.add("w1:p1", "reader")
    d = make_dispatcher(pointer_for=("reader",) if reader else ())
    screen = pane.screen
    stopped_screen = (
        Path(__file__)
        .with_name("data")
        .joinpath("claude-usage-stop.txt")
        .read_text(encoding="utf-8")
    )
    stopped = not after_submit
    reads = []

    def recorded_screen():
        reads.append(clock())
        return f"\x1b[31m{stopped_screen}\x1b[0m" if stopped else screen()

    monkeypatch.setattr(pane, "screen", recorded_screen)
    item = d.enqueue("reader", "report", source="sender")
    if after_submit:
        run(d, 1)
        assert item.state is State.VERIFYING
        stopped = True
        pane.status = "done"
    run(d, 10)
    assert item.state is (State.FILED if reader else State.WAITING)
    assert item.reason == f"usage stop in pane: {STOP}"
    status = d.status()
    pending = status["filed"] if reader else status["panes"]["w1:p1"]["pending"]
    assert pending[0]["reason"] == item.reason
    assert pane.submit_calls == int(after_submit)
    assert pane.enter_calls == 0
    if reader:
        assert mailbox.load(tmp_path, "reader")[0].state == "new"
    count = len(reads)
    run(d, 30)
    assert len(reads) == count
    stopped = False
    pane.status = "idle"
    run(d, 65)
    expected = pointer(item) if reader else item.text
    assert pane.submitted == [expected] * (1 + int(after_submit))
    assert item.state is (State.FILED if reader else State.SENT)


def test_unaffected_pane(herdr, make_dispatcher, run):
    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "report", source="sender")
    run(d, 5)
    assert pane.submitted == ["report"]
    assert item.state is State.SENT


@pytest.mark.parametrize("reader", [False, True])
def test_merged_items_are_preserved(herdr, make_dispatcher, run, monkeypatch, reader):
    pane = herdr.add("w1:p1", "reader")
    screen = pane.screen
    monkeypatch.setattr(
        pane, "screen", lambda: stopped(screen()) if pane.submit_calls else screen()
    )
    d = make_dispatcher(pointer_for=("reader",) if reader else (), merge_chars=6000)
    items = [d.enqueue("reader", text, source="sender") for text in ("one", "two")]
    run(d, 10)
    assert pane.submit_calls == 1
    for item in items:
        assert item.state is (State.FILED if reader else State.WAITING)
        assert item.reason == f"usage stop in pane: {STOP}"


def test_unconfirmed_observation_still_checks_screen(herdr, make_dispatcher, run, monkeypatch):
    from herdr_courier.core.transport import HerdrError

    pane = herdr.add("w1:p1")
    d = make_dispatcher()
    item = d.enqueue("w1:p1", "report", source="sender")
    run(d, 1)
    assert item.state is State.VERIFYING
    screen = pane.screen
    monkeypatch.setattr(pane, "screen", lambda: stopped(screen()))

    def unavailable(target):
        raise HerdrError("server_not_running", "cannot observe agent")

    monkeypatch.setattr(herdr, "agent_get", unavailable)
    run(d, 10)
    assert item.state is State.WAITING
    assert item.reason == f"usage stop in pane: {STOP}"
    assert pane.submit_calls == 1


def test_recorded_panes():
    data = Path(__file__).with_name("data")
    assert usage_stop((data / "claude-usage-stop.txt").read_text(encoding="utf-8")) == STOP
    codex = "■ You’ve hit your usage limit. Try again later.\n\n› Ask Codex to do anything\n"
    # Codex limits end as rejected through inputline.rejection, not held here.
    assert usage_stop(codex) is None


BOX = "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"


def test_a_later_prompt_ends_the_stop():
    switched = f"> hello\n  ⎿  {STOP}\n\n> /model\n  ⎿  Set model to Opus\n\n{BOX}"
    assert usage_stop(switched) is None
    assert usage_stop(switched.replace("❯ \n", "❯ [courier] 1 new message\n")) is None


def test_text_under_the_box_is_not_a_reply():
    assert usage_stop(f"● done\n\n{BOX}{STOP}\n") is None


@pytest.mark.parametrize("reader", [False, True])
def test_a_held_message_probes_a_stopped_pane(
    herdr, clock, make_dispatcher, run, monkeypatch, reader
):
    pane = herdr.add("w1:p1", "reader")
    screen = pane.screen
    monkeypatch.setattr(pane, "screen", lambda: stopped(screen()))
    d = make_dispatcher(pointer_for=("reader",) if reader else ())
    item = d.enqueue("reader", "report", source="sender")
    run(d, 30)
    assert pane.submit_calls == 0
    assert item.reason == f"usage stop in pane: {STOP}"
    run(d, USAGE_PROBE_SECONDS)
    assert pane.submit_calls == 1
    assert item.state is (State.FILED if reader else State.WAITING)
    assert item.reason == f"usage stop in pane: {STOP}"
    run(d, USAGE_PROBE_SECONDS - 60)
    assert pane.submit_calls == 1
