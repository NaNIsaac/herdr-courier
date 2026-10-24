"""Collision scenarios against a real herdr pane running ``dummy_agent.py``.

Opt-in: needs a live herdr session (HERDR_ENV=1), Windows, and COURIER_INTEGRATION=1. The test opens
its own tab without taking focus, and closes it afterwards; it never writes to any other pane.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from herdr_courier.core.queue import Dispatcher, State
from herdr_courier.core.transport import PaneHerdr

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            sys.platform == "win32"
            and os.environ.get("HERDR_ENV") == "1"
            and os.environ.get("COURIER_INTEGRATION") == "1"
        ),
        reason="needs Windows, a live herdr session and COURIER_INTEGRATION=1",
    ),
]

DUMMY = Path(__file__).with_name("dummy_agent.py")


def herdr(*args: str) -> dict:
    out = subprocess.run(
        ["herdr", *args], capture_output=True, encoding="utf-8", check=True, timeout=20
    ).stdout
    return json.loads(out)["result"] if out.strip() else {}


def submitted(log: Path) -> list[str]:
    if not log.exists():
        return []
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return [e["text"] for e in events if e["event"] == "submitted"]


def until(predicate, timeout: float, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


@pytest.fixture(scope="module")
def dummy(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    log = tmp / "dummy.jsonl"
    tab = herdr(
        "tab",
        "create",
        "--workspace",
        os.environ["HERDR_WORKSPACE_ID"],
        "--label",
        "courier-test",
        "--cwd",
        str(tmp),
        "--no-focus",
    )
    pane = tab["root_pane"]["pane_id"]
    try:
        time.sleep(1.5)
        herdr("pane", "run", pane, f'python -I "{DUMMY}" "{log}"')
        assert until(lambda: log.exists(), 15), "dummy agent did not start"
        herdr(
            "pane",
            "report-agent",
            pane,
            "--source",
            "courier-test",
            "--agent",
            "dummy",
            "--state",
            "idle",
        )
        yield pane, log
    finally:
        subprocess.run(
            ["herdr", "pane", "send-keys", pane, "ctrl+]"], capture_output=True, check=False
        )
        subprocess.run(
            ["herdr", "tab", "close", tab["tab"]["tab_id"]], capture_output=True, check=False
        )


@pytest.fixture
def dispatcher(tmp_path):
    return Dispatcher(PaneHerdr(), tmp_path / "journal.jsonl", log=lambda _: None)


def drive(d: Dispatcher, item, states, timeout: float) -> bool:
    def step():
        d.tick()
        return item.state in states

    return until(step, timeout, 0.5)


def test_raw_submit_merges_with_half_typed_input(dummy):
    # The bug itself, reproduced without the dispatcher: this is what herdr agent prompt does.
    pane, log = dummy
    before = len(submitted(log))
    herdr("pane", "send-text", pane, "half typed by isaac ")
    PaneHerdr().submit(pane, "[lane] status report")
    assert until(lambda: len(submitted(log)) > before, 5)
    assert submitted(log)[-1] == "half typed by isaac [lane] status report"


def test_dispatcher_waits_for_human_and_sends_alone(dummy, dispatcher):
    pane, log = dummy
    before = len(submitted(log))
    herdr("pane", "send-text", pane, "half typed by isaac ")
    item = dispatcher.enqueue(pane, "[lane] status report", source="test")
    assert not drive(dispatcher, item, {State.SENT, State.FAILED}, 5)
    assert item.state is State.WAITING
    assert item.reason.startswith("input typed")
    assert len(submitted(log)) == before

    herdr("pane", "send-keys", pane, "ctrl+u")
    assert drive(dispatcher, item, {State.SENT, State.FAILED}, 10), item.reason
    assert item.state is State.SENT
    assert submitted(log)[before:] == ["[lane] status report"]


def test_hold_keeps_dispatcher_out_until_release(dummy, dispatcher):
    pane, log = dummy
    before = len(submitted(log))
    dispatcher.hold(pane, owner="test", reason="isaac on phone", ttl=None)
    item = dispatcher.enqueue(pane, "[lane] after hold", source="test")
    assert not drive(dispatcher, item, {State.SENT}, 4)
    assert item.reason == "hold: isaac on phone"
    dispatcher.release(pane)
    assert drive(dispatcher, item, {State.SENT, State.FAILED}, 10), item.reason
    assert submitted(log)[before:] == ["[lane] after hold"]


def test_serve_and_send_end_to_end(dummy, tmp_path):
    # A real hcd (courier serve) on its own state directory (not the user's), driven by the real courier.
    pane, log = dummy
    env = {**os.environ, "COURIER_STATE_DIR": str(tmp_path / "courier-state"), "PYTHONUTF8": "1"}
    server = subprocess.Popen(
        [
            str(Path(sys.executable).with_name("hcd.exe")),
            "--transport",
            "pane",
            "--tick",
            "0.2",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
    )
    try:
        endpoint = tmp_path / "courier-state" / "endpoint.json"
        assert until(endpoint.exists, 15), "courier serve did not start"
        before = len(submitted(log))
        herdr("pane", "send-text", pane, "isaac half typed ")

        def courier(*args):
            return subprocess.run(
                [sys.executable, "-m", "herdr_courier.cli", *args],
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )

        queued = courier("send", pane, "[lane] via server")
        assert queued.returncode == 0, queued.stderr
        time.sleep(3)
        assert len(submitted(log)) == before
        assert "input typed" in courier("status").stdout
        assert "ok   dispatcher" in courier("doctor").stdout

        herdr("pane", "send-keys", pane, "ctrl+u")
        assert until(lambda: len(submitted(log)) > before, 15)
        assert submitted(log)[before:] == ["[lane] via server"]
    finally:
        server.terminate()
        server.wait(timeout=10)
