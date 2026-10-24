from dataclasses import dataclass, field

import pytest

from herdr_courier.core.queue import Dispatcher
from herdr_courier.core.transport import HerdrError

RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 40 + "\x1b[0m"
DIM_GLYPH = "\x1b[0m\x1b[38;2;153;153;153m❯ \x1b[0m"


@dataclass
class FakePane:
    status: str = "idle"
    session: str = "s1"
    focused: bool = False
    box: str = ""
    placeholder: str = 'Try "how does <filepath> work?"'
    dialog: bool = False
    submit_mode: str = "ok"
    submitted: list[str] = field(default_factory=list)
    submit_calls: int = 0

    def screen(self) -> str:
        if self.dialog:
            return "Do you trust this folder?\n❯ No, exit\n  Yes, I trust this folder\n"
        if self.box:
            line = "❯ " + self.box.replace("\n", "\r\n  ")
        else:
            line = DIM_GLYPH + "\x1b[2m" + self.placeholder + "\x1b[0m"
        return f"● earlier output\r\n\r\n{RULE}\r\n{line}\r\n{RULE}\r\n  auto mode on\r\n"


class FakeHerdr:
    """Models the collision: a submit lands after whatever is already in the box."""

    def __init__(self) -> None:
        self.panes: dict[str, FakePane] = {}
        self.names: dict[str, str] = {}

    def add(self, pane_id: str, name: str | None = None, **kw) -> FakePane:
        self.panes[pane_id] = FakePane(**kw)
        if name:
            self.names[name] = pane_id
        return self.panes[pane_id]

    def _pane(self, target: str) -> tuple[str, FakePane]:
        pane_id = self.names.get(target, target)
        if pane_id not in self.panes:
            raise HerdrError("agent_not_found", f"agent target {target} not found")
        return pane_id, self.panes[pane_id]

    def agent_get(self, target: str) -> dict:
        pane_id, pane = self._pane(target)
        return {
            "pane_id": pane_id,
            "agent_status": pane.status,
            "focused": pane.focused,
            "agent_session": {"value": pane.session},
        }

    def agent_list(self) -> list[dict]:
        return [self.agent_get(p) for p in self.panes]

    def read_ansi(self, pane_id: str, lines: int = 12) -> str:
        return self._pane(pane_id)[1].screen()

    def submit(self, pane_id: str, text: str) -> None:
        pane = self._pane(pane_id)[1]
        pane.submit_calls += 1
        match pane.submit_mode:
            case "ok":
                pane.submitted.append(pane.box + text)
                pane.box = ""
                pane.status = "working"
            case "stuck":
                pane.box += text
            case code:
                raise HerdrError(code, "fake failure")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def state_dir(monkeypatch, tmp_path_factory):
    """Keep every test away from the user's state directory and its config.toml."""
    path = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("COURIER_STATE_DIR", str(path))
    return path


@pytest.fixture
def herdr() -> FakeHerdr:
    return FakeHerdr()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_dispatcher(herdr, clock, tmp_path):
    def make() -> Dispatcher:
        return Dispatcher(herdr, tmp_path / "journal.jsonl", clock=clock, log=lambda _: None)

    return make


@pytest.fixture
def run(clock):
    """Advance the fake clock in half-second ticks, like the dispatcher's worker."""

    def advance(dispatcher: Dispatcher, seconds: float) -> None:
        end = clock.now + seconds
        while clock.now < end:
            dispatcher.tick()
            clock.now += 0.5
        dispatcher.tick()

    return advance
