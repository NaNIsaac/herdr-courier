"""The send conditions, as a pure function of one observation of the target pane."""

from dataclasses import dataclass

from .inputline import InputLine, LineState

# Claude Code queues a prompt that arrives while it is working, so "working" is safe as long
# as the input box is empty. "blocked" means a dialog is up and Enter would answer it.
SENDABLE = frozenset({"idle", "done", "working"})
DEBOUNCE = 1.0
DEBOUNCE_FOCUSED = 3.0


@dataclass(frozen=True)
class Observation:
    status: str
    session: str | None
    focused: bool
    line: InputLine
    name: str | None = None
    # herdr's state_change_seq: it moves on a status change too short to be seen between reads.
    state_seq: int | None = None

    @property
    def state(self) -> tuple[str, int | None]:
        return self.status, self.state_seq


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    fatal: bool = False


def check(
    obs: Observation,
    *,
    session: str | None,
    hold: str | None,
    empty_for: float | None,
    settle: float | None = None,
    settled_for: float | None = None,
) -> Verdict:
    """Decide whether to send now.

    ``empty_for`` is how long the input box has been seen empty without interruption, or None
    if the latest read was not empty. A focused pane gets a longer debounce because focus is
    the only hint herdr gives that a human may be about to type there; it is not per client,
    so it is a hint and not a lock.

    ``settle`` is set for a pane that also takes queued Ovis chat messages: the message goes only
    between turns, once the agent state (``settled_for``) has not changed for that many seconds.
    Ovis sends its queued message as soon as the turn ends and leaves the paste unsent in the box
    if the agent starts another turn before its Enter; a prompt Claude Code queued during the
    turn starts one at exactly that moment.
    """
    # A pane that had a session and now reports none is not the agent the message was for.
    if session and obs.session != session:
        return Verdict(
            False, f"identity_changed: session {obs.session} is not {session}", fatal=True
        )
    if obs.status not in SENDABLE:
        return Verdict(False, f"agent {obs.status}")
    if hold:
        return Verdict(False, f"hold: {hold}")
    if settle is not None:
        if obs.status == "working":
            return Verdict(
                False, "between turns: Ovis may send a queued message when this turn ends"
            )
        if settled_for is None or settled_for < settle:
            return Verdict(False, f"settle: agent state must stay unchanged for {settle:g} s")
    # Reasons stay constant while the condition holds, so the journal records changes of
    # condition rather than every keystroke; the live evidence is in the status view.
    if obs.line.state is LineState.TYPED:
        return Verdict(False, "input typed: human text in the input box")
    if obs.line.state is LineState.UNKNOWN:
        return Verdict(False, f"input unknown: {obs.line.evidence}")
    need = DEBOUNCE_FOCUSED if obs.focused else DEBOUNCE
    if empty_for is None or empty_for < need:
        return Verdict(False, f"debounce: input must stay empty for {need:.0f} s")
    return Verdict(True, obs.line.evidence)
