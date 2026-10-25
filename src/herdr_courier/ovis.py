"""Ovis, the herdr browser and phone client: whether it runs, from its unauthenticated health route.

Ovis 0.4.14 has no route that tells another program which panes hold queued chat messages; those
go only to the browser connection that queued them. courier therefore treats every pane in
``ovis.panes`` as possibly holding one while Ovis answers.
"""

import json
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

# How long one answer stands. Ovis is started and stopped by hand, rarely.
RECHECK = 30.0
PROBE_TIMEOUT = 1.0


def health(url: str, timeout: float = 3.0) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/api/health", timeout=timeout) as resp:
        return json.loads(resp.read())


class Presence:
    """Whether Ovis answers at ``url``, asked at most once per RECHECK seconds."""

    def __init__(
        self,
        url: str,
        *,
        probe: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self._probe = probe or (lambda u: health(u, PROBE_TIMEOUT))
        self._clock = clock
        self._lock = threading.Lock()
        self._up = False
        self._asked: float | None = None

    def up(self) -> bool:
        if not self.url:
            return False
        with self._lock:
            now = self._clock()
            if self._asked is None or now - self._asked >= RECHECK:
                try:
                    self._up = bool(self._probe(self.url).get("ok"))
                except (OSError, ValueError, AttributeError):
                    self._up = False
                self._asked = now
            return self._up


def settle_rule(
    presence: Presence, panes: tuple[str, ...], settle: float
) -> Callable[[str | None], float | None]:
    """The dispatcher's ``settle_for``: ``settle`` for a listed agent while Ovis runs, else None."""
    names = frozenset(panes)

    def settle_for(name: str | None) -> float | None:
        # Ovis is asked only for a listed pane, so other panes never wait on its probe.
        return settle if name in names and presence.up() else None

    return settle_for
