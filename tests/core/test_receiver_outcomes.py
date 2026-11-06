import pytest

from herdr_courier.core.inputline import LineState, classify, rejection
from herdr_courier.core.queue import Dispatcher, State


def test_codex_limit_is_not_reported_as_sent(herdr, make_dispatcher, run, monkeypatch):
    pane = herdr.add("w1", terminal="term")
    original = herdr.read_ansi

    def screen(pane_id, lines=12):
        if pane.submitted:
            return "■ You’ve hit your usage limit. Try again later.\n\n› \n\n"
        return original(pane_id, lines)

    monkeypatch.setattr(herdr, "read_ansi", screen)
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1", "test message", source="test")
    run(dispatcher, 10)
    assert item.state.value == "rejected"


def test_single_line_owned_input_is_retried_then_marked_ghost(
    herdr, make_dispatcher, run, monkeypatch
):
    pane = herdr.add("w1", submit_mode="stuck", terminal="term")
    keys = []
    monkeypatch.setattr(herdr, "enter", lambda target: keys.append(target), raising=False)
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1", "single line", source="test")
    run(dispatcher, 20)
    assert keys == ["w1"]
    assert item.state.value == "ghost"
    assert pane.box == "single line"
    assert pane.submit_calls == 1


def test_known_codex_placeholder_has_a_safe_footer_fallback():
    line = classify(
        "output\n› \x1b[2mAsk Codex to do anything\x1b[0m\n"
        "  GPT-6.1 default · project\n  ? for shortcuts\n"
    )
    assert line.state is LineState.EMPTY


@pytest.mark.parametrize(
    "text",
    [
        "\n› typed human text\n  ? for shortcuts\n",
        "\n› Ask Codex to do anything\n  ? for shortcuts\n",
        "Choose:\n› \x1b[2mAsk Codex to do anything\x1b[0m\n  2. Cancel\n  ? for shortcuts\n",
    ],
)
def test_codex_fallback_never_guesses_empty_for_human_or_dialog(text):
    assert classify(text).state is not LineState.EMPTY


def test_owned_plain_codex_multiline_is_only_typed_on_complete_match():
    screen = "\n› First\n  Second\n\n  ? for shortcuts\n"
    assert classify(screen).state is LineState.UNKNOWN
    assert classify(screen, queued="First\nSecond").state is LineState.TYPED
    assert classify(screen, queued="First").state is LineState.UNKNOWN
    assert classify("\n› 1. Yes\n  2. No\n\n", queued="1. Yes\n2. No").state is LineState.UNKNOWN


def test_edited_owned_input_is_ghost_even_when_agent_status_is_working(herdr, make_dispatcher, run):
    pane = herdr.add("w1", submit_mode="stuck", terminal="term")
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1", "courier text", source="test")
    run(dispatcher, 2)
    pane.box += " human edit"
    pane.status = "working"
    run(dispatcher, 10)
    assert item.state is State.GHOST
    assert not pane.enter_calls


@pytest.mark.parametrize(
    "text",
    [
        "\n› ■ You’ve hit your usage limit\n\n",
        "■ You’ve hit your usage limit\n› a newer request\nresponse\n\n› \n\n",
    ],
)
def test_limit_text_in_input_or_earlier_history_is_not_current_rejection(text):
    assert rejection(text) is None


def test_unchanged_old_limit_does_not_reject_first_send_after_reset(
    herdr, make_dispatcher, run, monkeypatch
):
    pane = herdr.add("w1", terminal="term")
    monkeypatch.setattr(
        herdr, "read_ansi", lambda *args, **kwargs: "■ You’ve hit your usage limit\n\n› \n\n"
    )
    dispatcher = make_dispatcher()
    item = dispatcher.enqueue("w1", "after reset", source="test")
    run(dispatcher, 5)
    assert item.state is State.SENT
    assert pane.submit_calls == 1


def test_rejected_and_ghost_survive_restart_without_replaying(herdr, clock, tmp_path, run):
    pane = herdr.add("w1", submit_mode="stuck", terminal="term")
    dispatcher = Dispatcher(herdr, tmp_path / "journal", clock=clock, log=lambda _: None)
    item = dispatcher.enqueue("w1", "owned text", source="test")
    run(dispatcher, 15)
    assert item.state is State.GHOST
    rejected = dispatcher.enqueue("w1", "rejected", source="test")
    dispatcher._finish(rejected, State.REJECTED, "codex_usage_limit")
    before = pane.submit_calls
    restored = Dispatcher(herdr, tmp_path / "journal", clock=clock, log=lambda _: None)
    run(restored, 15)
    assert restored.items[item.id].state is State.GHOST
    assert restored.items[rejected.id].state is State.REJECTED
    assert pane.submit_calls == before
