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
) -> Verdict:
    """Decide whether to send now.

    ``empty_for`` is how long the input box has been seen empty without interruption, or None
    if the latest read was not empty. A focused pane gets a longer debounce because focus is
    the only hint herdr gives that a human may be about to type there; it is not per client,
    so it is a hint and not a lock.
    """
    if session and obs.session and obs.session != session:
        return Verdict(
            False, f"identity_changed: session {obs.session} is not {session}", fatal=True
        )
    if obs.status not in SENDABLE:
        return Verdict(False, f"agent {obs.status}")
    if hold:
        return Verdict(False, f"hold: {hold}")
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
