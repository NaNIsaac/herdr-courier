"""courier against Ovis' automatic send of a queued chat message, on one simulated clock.

MockOvis follows ``dispatchPendingText`` in Ovis 0.4.14 (server/core/pending.ts) on its automatic
path for a Claude Code pane: once the turn ends it refuses a filled box, pastes, waits
SUBMIT_DELAY_MS, reads the status again and, if the agent is working by then, stops before Enter
and leaves the paste in the box (``submit_changed``). ClaudeHerdr adds Claude Code's own queue: a
prompt entered during a turn starts the next turn right after this one ends.
"""

from conftest import FakeHerdr, FakePane

from herdr_courier import ovis
from herdr_courier.core.queue import Dispatcher, State

STEP = 0.02
TICK = 0.5
TURN = 4.0
SUBMIT_DELAY = 0.12
# Between Claude Code's end of turn and the start of the prompt it queued meanwhile.
NEXT_QUEUED = 0.05
OWNER = "owner text written in Ovis chat during the turn"
BOSS = "[boss] message for the owner"


class ClaudeHerdr(FakeHerdr):
    def __init__(self, clock) -> None:
        super().__init__()
        self.clock = clock
        self.seq = 0
        self.queued: list[str] = []
        self.prompts: list[str] = []
        self.events: list[tuple[float, str]] = []

    def agent_get(self, target: str) -> dict:
        return super().agent_get(target) | {"state_change_seq": self.seq}

    def status(self, pane: FakePane, status: str) -> None:
        pane.status = status
        self.seq += 1
        if status == "working":
            self.events.append((self.clock.now + TURN, "end"))

    def enter(self, pane: FakePane, text: str) -> None:
        pane.box = ""
        if pane.status == "working":
            self.queued.append(text)
        else:
            self.prompts.append(text)
            self.status(pane, "working")

    def submit(self, pane_id: str, text: str) -> None:
        pane = self.panes[pane_id]
        pane.submit_calls += 1
        self.enter(pane, pane.box + text)

    def due(self, pane: FakePane) -> None:
        for event in [e for e in self.events if e[0] <= self.clock.now]:
            self.events.remove(event)
            if event[1] == "end":
                self.status(pane, "done")
                if self.queued:
                    self.events.append((self.clock.now + NEXT_QUEUED, "next"))
            elif self.queued:
                # Claude Code starts its queued prompt whatever the box holds.
                self.prompts.append(self.queued.pop(0))
                self.status(pane, "working")


class MockOvis:
    def __init__(self, herdr: ClaudeHerdr, pane: FakePane, text: str) -> None:
        self.herdr, self.pane, self.text = herdr, pane, text
        self.state = "queued"
        self.pasted_at = 0.0

    def step(self, now: float) -> None:
        if self.state == "queued" and self.pane.status in ("idle", "done"):
            if self.pane.box:
                return
            self.pane.box += self.text
            self.pasted_at, self.state = now, "pasted"
        elif self.state == "pasted" and now >= self.pasted_at + SUBMIT_DELAY:
            if self.pane.status == "working":
                self.state = "uncertain"
            else:
                self.herdr.enter(self.pane, self.pane.box)
                self.state = "sent"


def scene(clock, tmp_path, settle_for=None):
    """secretary is mid-turn with an Ovis message queued when boss's message arrives."""
    herdr = ClaudeHerdr(clock)
    pane = herdr.add("w1:p4E", "secretary")
    herdr.status(pane, "working")
    owner = MockOvis(herdr, pane, OWNER)
    d = Dispatcher(
        herdr, tmp_path / "journal.jsonl", clock=clock, log=lambda _: None, settle_for=settle_for
    )
    item = d.enqueue("secretary", BOSS, source="w1:p1S")
    return herdr, pane, owner, d, item


def run(clock, herdr, pane, owner, d, seconds):
    end = clock.now + seconds
    next_tick = clock.now
    while clock.now < end:
        herdr.due(pane)
        owner.step(clock.now)
        if clock.now >= next_tick:
            d.tick()
            next_tick += TICK
        clock.now = round(clock.now + STEP, 6)


def ovis_up(answer=None):
    return ovis.Presence(
        "http://ovis.test", probe=lambda _: {"ok": True} if answer is None else answer
    )


def test_reproduced_queued_courier_prompt_orphans_the_ovis_paste(clock, tmp_path):
    herdr, pane, owner, d, item = scene(clock, tmp_path)
    run(clock, herdr, pane, owner, d, 12)
    assert item.state is State.SENT
    assert owner.state == "uncertain"
    assert pane.box == OWNER
    assert herdr.prompts == [BOSS]


def test_between_turns_rule_lets_ovis_go_first(clock, tmp_path):
    rule = ovis.settle_rule(ovis_up(), ("secretary",), 3.0)
    herdr, pane, owner, d, item = scene(clock, tmp_path, rule)
    run(clock, herdr, pane, owner, d, 3)
    assert item.state is State.WAITING
    assert item.reason.startswith("between turns")
    run(clock, herdr, pane, owner, d, 12)
    assert owner.state == "sent"
    assert item.state is State.SENT
    assert pane.box == ""
    assert herdr.prompts == [OWNER, BOSS]


def test_settle_waits_out_a_turn_end_before_writing(clock, tmp_path):
    rule = ovis.settle_rule(ovis_up(), ("secretary",), 3.0)
    herdr, pane, owner, d, item = scene(clock, tmp_path, rule)
    owner.state = "none"
    run(clock, herdr, pane, owner, d, TURN + 2.5)
    assert item.state is State.WAITING
    assert item.reason.startswith("settle")
    run(clock, herdr, pane, owner, d, 1.5)
    assert herdr.prompts == [BOSS]


def test_without_ovis_nothing_changes(clock, tmp_path):
    def sends(settle_for):
        clock.now = 1000.0
        herdr, pane, owner, d, _ = scene(clock, tmp_path / str(len(seen)), settle_for)
        run(clock, herdr, pane, owner, d, 12)
        return herdr.prompts, pane.box, owner.state

    def refused(url):
        raise ConnectionRefusedError(url)

    seen = []
    seen.append(sends(None))
    down = ovis.Presence("http://ovis.test", probe=refused)
    seen.append(sends(ovis.settle_rule(down, ("secretary",), 3.0)))
    seen.append(sends(ovis.settle_rule(ovis_up({"ok": False}), ("secretary",), 3.0)))
    seen.append(sends(ovis.settle_rule(ovis.Presence(""), ("secretary",), 3.0)))
    assert seen == [seen[0]] * 4


def test_other_panes_are_not_held_and_ovis_is_not_asked(clock, tmp_path):
    asked = []
    presence = ovis.Presence(
        "http://ovis.test", probe=lambda url: asked.append(url) or {"ok": True}
    )
    herdr, pane, owner, d, _ = scene(clock, tmp_path, ovis.settle_rule(presence, ("boss",), 3.0))
    run(clock, herdr, pane, owner, d, 2)
    assert herdr.queued == [BOSS]
    assert asked == []


def test_presence_is_asked_once_per_recheck():
    now = [0.0]
    asked = []
    presence = ovis.Presence(
        "http://ovis.test",
        probe=lambda url: asked.append(url) or {"ok": True},
        clock=lambda: now[0],
    )
    assert presence.up() and presence.up()
    now[0] = ovis.RECHECK
    assert presence.up()
    assert len(asked) == 2
