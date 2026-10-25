"""Per-pane FIFO queues, the delivery state machine, and the journal.

One worker thread calls ``tick``; it is the only writer to any pane, which is what makes the
per-pane queue a lock. Request threads only enqueue, hold, release, cancel and read status.
"""

import json
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .gate import Observation, check
from .inputline import InputLine, LineState, classify, holds
from .transport import Herdr, HerdrError

VERIFY_SECONDS = 5.0
MAX_ATTEMPTS = 5
KEEP_FINISHED = 200
# Each restart closes one journal generation; the older ones are the only delivery history.
JOURNAL_KEEP = 10
# herdr rejects these before writing any byte, so a retry cannot duplicate a message.
SUBMIT_RETRYABLE = frozenset({"agent_blocked", "agent_not_ready"})
TARGET_GONE = frozenset({"agent_not_found", "pane_not_found"})


class State(StrEnum):
    QUEUED = "queued"
    WAITING = "waiting"
    SUBMITTING = "submitting"
    VERIFYING = "verifying"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


FINAL = frozenset({State.SENT, State.FAILED, State.CANCELLED})
# From the moment the write may have started; such an item is never written again.
IN_FLIGHT = frozenset({State.SUBMITTING, State.VERIFYING})


@dataclass
class Item:
    id: str
    target: str
    pane_id: str
    session: str | None
    text: str
    source: str
    human: bool
    created: float
    state: State = State.QUEUED
    reason: str = ""
    attempts: int = 0
    updated: float = 0.0
    retry_at: float = 0.0
    verify_until: float = 0.0
    client_id: str | None = None


@dataclass
class Hold:
    owner: str
    reason: str
    until: float | None
    until_wall: float | None = None
    restored: bool = False


@dataclass
class PaneWatch:
    empty_since: float | None = None
    last: str = ""


class DispatchError(Exception):
    pass


class Dispatcher:
    def __init__(self, herdr: Herdr, journal: Path, *, clock=time.monotonic, log=print) -> None:
        self.herdr = herdr
        self.journal = journal
        self.clock = clock
        self.log = log
        self.lock = threading.Condition()
        self.items: dict[str, Item] = {}
        self.queues: dict[str, list[str]] = {}
        self.holds: dict[str, Hold] = {}
        self.watch: dict[str, PaneWatch] = {}
        self._replay()

    def enqueue(
        self,
        target: str,
        text: str,
        *,
        source: str,
        human: bool = False,
        client_id: str | None = None,
    ) -> Item:
        if not text.strip():
            raise DispatchError("empty message")
        if client_id and (known := self._by_client_id(client_id)):
            return known
        try:
            agent = self.herdr.agent_get(target)
        except HerdrError as exc:
            raise DispatchError(f"cannot resolve target {target!r}: {exc}") from exc
        session = (agent.get("agent_session") or {}).get("value")
        item = Item(
            id=uuid.uuid4().hex[:10],
            target=target,
            pane_id=agent["pane_id"],
            session=session,
            text=text,
            source=source,
            human=human,
            created=time.time(),
            client_id=client_id,
        )
        with self.lock:
            # A retried request may have raced the first one while the target was resolved.
            if client_id and (known := self._by_client_id(client_id)):
                return known
            self._record(item, "queued", full=True)
            self.items[item.id] = item
            queue = self.queues.setdefault(item.pane_id, [])
            if human:
                # Human messages go ahead of queued agent traffic but behind an in-flight one.
                pos = sum(
                    1 for i in queue if self.items[i].human or self.items[i].state in IN_FLIGHT
                )
                queue.insert(pos, item.id)
            else:
                queue.append(item.id)
            self.lock.notify_all()
        return item

    def _by_client_id(self, client_id: str) -> Item | None:
        with self.lock:
            return next((i for i in self.items.values() if i.client_id == client_id), None)

    def hold(self, target: str, *, owner: str, reason: str, ttl: float | None) -> str:
        pane_id = self.resolve(target)
        hold = Hold(
            owner,
            reason or f"held by {owner}",
            None if ttl is None else self.clock() + ttl,
            None if ttl is None else time.time() + ttl,
        )
        with self.lock:
            self._journal_hold(pane_id, hold)
            self.holds[pane_id] = hold
        return pane_id

    def release(self, target: str) -> str:
        pane_id = self.resolve(target)
        with self.lock:
            self._journal({"event": "release", "pane_id": pane_id})
            self.holds.pop(pane_id, None)
        return pane_id

    def cancel(self, item_id: str) -> Item:
        with self.lock:
            item = self.items.get(item_id)
            if item is None:
                raise DispatchError(f"no item {item_id}")
            if item.state in FINAL or item.state in IN_FLIGHT:
                raise DispatchError(f"item {item_id} is {item.state}, too late to cancel")
            self._finish(item, State.CANCELLED, "cancelled by request")
            return item

    def wait(self, item_id: str, timeout: float) -> Item:
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                item = self.items.get(item_id)
                if item is None:
                    raise DispatchError(f"no item {item_id}")
                left = deadline - time.monotonic()
                if item.state in FINAL or left <= 0:
                    return item
                self.lock.wait(min(left, 1.0))

    def status(self, pane: str | None = None) -> dict[str, Any]:
        with self.lock:
            now = self.clock()
            panes = {}
            wanted = sorted(set(self.queues) | set(self.holds)) if pane is None else [pane]
            for pane_id in wanted:
                hold = self.holds.get(pane_id)
                pending = [asdict(self.items[i]) for i in self.queues.get(pane_id, [])]
                panes[pane_id] = {
                    "pending": pending,
                    "hold": None
                    if hold is None
                    else {
                        "owner": hold.owner,
                        "reason": hold.reason,
                        "seconds_left": None if hold.until is None else round(hold.until - now),
                        "restored": hold.restored,
                    },
                    "observed": self.watch.get(pane_id, PaneWatch()).last,
                }
            finished = sorted(
                (
                    i
                    for i in self.items.values()
                    if i.state in FINAL and (pane is None or i.pane_id == pane)
                ),
                key=lambda i: i.updated,
            )[-20:]
            return {"panes": panes, "recent": [asdict(i) for i in finished]}

    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            for pane_id, hold in list(self.holds.items()):
                if hold.until is not None and hold.until <= now:
                    self._journal({"event": "hold_expired", "pane_id": pane_id})
                    del self.holds[pane_id]
            heads = [
                (pane_id, self.items[queue[0]]) for pane_id, queue in self.queues.items() if queue
            ]
        for pane_id, item in heads:
            try:
                self._advance(pane_id, item)
            except Exception as exc:  # noqa: BLE001 - one bad pane must not stop the others
                with self.lock:
                    self._stall(item, f"dispatcher error: {exc!r}")

    def _advance(self, pane_id: str, item: Item) -> None:
        now = self.clock()
        if item.retry_at > now:
            return
        obs = self._observe(item)
        if obs is None:
            return
        with self.lock:
            if item.state in FINAL:
                return
            if item.state in IN_FLIGHT:
                # SUBMITTING here means an earlier write returned but its VERIFYING record was
                # not written; confirm it like any other write instead of writing again.
                self._verify(item, obs, now)
                return
            verdict = check(
                obs,
                session=item.session,
                hold=self._hold_reason(pane_id, now),
                empty_for=self._empty_for(pane_id, obs.line, now),
            )
            if verdict.fatal:
                self._finish(item, State.FAILED, verdict.reason)
                return
            if not verdict.ok:
                self._set(item, State.WAITING, verdict.reason)
                return
        # Observe again right before writing, all conditions and not only the box, to narrow
        # the window in which a human, a hold or a new agent could appear.
        final = self._observe(item)
        if final is None:
            return
        with self.lock:
            if item.state in FINAL:
                return
            now = self.clock()
            verdict = check(
                final,
                session=item.session,
                hold=self._hold_reason(pane_id, now),
                empty_for=math.inf if final.line.state is LineState.EMPTY else None,
            )
            if verdict.fatal:
                self._finish(item, State.FAILED, verdict.reason)
                return
            if not verdict.ok:
                self._empty_for(pane_id, final.line, now)
                self._set(item, State.WAITING, f"{verdict.reason} (at final check)")
                return
            # Journalled before the write: after a crash from here on the item is replayed as
            # FAILED and a cancel is refused, so the message can never be written twice.
            self._set(item, State.SUBMITTING, "writing to the pane")
            # The next message must see the box empty for a full debounce after this write.
            self.watch.setdefault(pane_id, PaneWatch()).empty_since = None
        try:
            self.herdr.submit(pane_id, item.text)
        except Exception as exc:  # noqa: BLE001 - every failure here must end the item
            with self.lock:
                item.attempts += 1
                code = exc.code if isinstance(exc, HerdrError) else None
                if code in SUBMIT_RETRYABLE and item.attempts < MAX_ATTEMPTS:
                    item.retry_at = self.clock() + 2.0**item.attempts
                    self._set(item, State.WAITING, f"retry {item.attempts}: {exc}")
                else:
                    # Anything else may have written part of the message; resending could
                    # duplicate it, so stop and report.
                    self._finish(item, State.FAILED, f"submit failed: {exc!r}")
            return
        with self.lock:
            item.verify_until = self.clock() + VERIFY_SECONDS
            self._set(item, State.VERIFYING, "submitted, confirming")

    def _verify(self, item: Item, obs: Observation, now: float) -> None:
        if obs.status in ("working", "blocked"):
            self._finish(item, State.SENT, f"agent {obs.status} after submit")
        elif obs.line.state is LineState.EMPTY:
            self._finish(item, State.SENT, f"input box cleared ({obs.line.evidence})")
        elif now < item.verify_until:
            return
        elif holds(obs.line, item.text):
            # Enter did not take. Leave the text for a human to see; never resend.
            self._finish(item, State.FAILED, "stuck_in_input: text is still in the input box")
        else:
            self._finish(item, State.SENT, f"unconfirmed: {obs.line.evidence}")

    def _observe(self, item: Item) -> Observation | None:
        try:
            agent = self.herdr.agent_get(item.pane_id)
            line = classify(self.herdr.read_ansi(item.pane_id))
        except HerdrError as exc:
            with self.lock:
                if item.state in IN_FLIGHT:
                    if self.clock() >= item.verify_until:
                        self._finish(item, State.SENT, f"unconfirmed: cannot observe: {exc}")
                    return None
                if exc.code in TARGET_GONE:
                    item.attempts += 1
                    if item.attempts >= MAX_ATTEMPTS:
                        self._finish(item, State.FAILED, f"target gone: {exc}")
                        return None
                    item.retry_at = self.clock() + 2.0**item.attempts
                self._set(item, State.WAITING, f"cannot observe: {exc}")
            return None
        obs = Observation(
            status=agent.get("agent_status", "unknown"),
            session=(agent.get("agent_session") or {}).get("value"),
            focused=bool(agent.get("focused")),
            line=line,
        )
        with self.lock:
            self.watch.setdefault(
                item.pane_id, PaneWatch()
            ).last = (
                f"{obs.status}, {'focused, ' if obs.focused else ''}{line.state}: {line.evidence}"
            )
        return obs

    def _empty_for(self, pane_id: str, line: InputLine, now: float) -> float | None:
        watch = self.watch.setdefault(pane_id, PaneWatch())
        if line.state is not LineState.EMPTY:
            watch.empty_since = None
            return None
        if watch.empty_since is None:
            watch.empty_since = now
        return now - watch.empty_since

    def _hold_reason(self, pane_id: str, now: float) -> str | None:
        hold = self.holds.get(pane_id)
        if hold is None or (hold.until is not None and hold.until <= now):
            return None
        return hold.reason

    def resolve(self, target: str) -> str:
        """Pane id for an agent name or pane id."""
        if target.count(":") == 1 and target.split(":")[1].startswith("p"):
            return target
        try:
            return self.herdr.agent_get(target)["pane_id"]
        except HerdrError as exc:
            raise DispatchError(f"cannot resolve target {target!r}: {exc}") from exc

    def _stall(self, item: Item, reason: str) -> None:
        """Record a problem without leaving an in-flight state, which would allow a second write."""
        if item.state in FINAL:
            return
        self._set(item, item.state if item.state in IN_FLIGHT else State.WAITING, reason)

    def _set(self, item: Item, state: State, reason: str) -> None:
        if item.state is state and item.reason == reason:
            return
        # Journal first: if that write fails the item keeps its old state, so a SUBMITTING
        # record that cannot be written stops the pane write instead of following it.
        self._journal(
            {
                "event": "item",
                "id": item.id,
                "state": state.value,
                "reason": reason,
                "attempts": item.attempts,
            }
        )
        item.state = state
        item.reason = reason
        item.updated = time.time()
        self._log(item)
        self.lock.notify_all()

    def _finish(self, item: Item, state: State, reason: str) -> None:
        queue = self.queues.get(item.pane_id, [])
        if item.id in queue:
            queue.remove(item.id)
        self._set(item, state, reason)
        finished = [i for i in self.items.values() if i.state in FINAL]
        for old in sorted(finished, key=lambda i: i.updated)[:-KEEP_FINISHED]:
            del self.items[old.id]

    def _record(self, item: Item, reason: str, *, full: bool = False) -> None:
        entry: dict[str, Any] = {
            "event": "item",
            "id": item.id,
            "state": item.state.value,
            "reason": reason,
            "attempts": item.attempts,
        }
        if full:
            entry["item"] = asdict(item)
        self._journal(entry)
        self._log(item)

    def _log(self, item: Item) -> None:
        self.log(
            f"{time.strftime('%H:%M:%S')} {item.id} {item.target}({item.pane_id}) "
            f"{item.state}: {item.reason}"
        )

    def _journal_hold(self, pane_id: str, hold: Hold) -> None:
        self._journal(
            {
                "event": "hold",
                "pane_id": pane_id,
                "owner": hold.owner,
                "reason": hold.reason,
                "until_wall": hold.until_wall,
            }
        )

    def _journal(self, entry: dict[str, Any]) -> None:
        entry["t"] = round(time.time(), 3)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _close_generation(self) -> None:
        """Rename the journal to ``<stem>.<UTC stamp>.jsonl`` and keep the newest JOURNAL_KEEP.

        A ``journal.prev.jsonl`` left by 0.2.0 does not match the stamp pattern and stays.
        """
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        stem = self.journal.stem
        dest = self.journal.with_name(f"{stem}.{stamp}.jsonl")
        n = 1
        while dest.exists():
            dest = self.journal.with_name(f"{stem}.{stamp}-{n}.jsonl")
            n += 1
        self.journal.replace(dest)
        for old in sorted(self.journal.parent.glob(f"{stem}.[0-9]*.jsonl"))[:-JOURNAL_KEEP]:
            old.unlink(missing_ok=True)

    def _replay(self) -> None:
        """Rebuild pending items and holds after a restart and compact the journal."""
        if not self.journal.exists():
            return
        items: dict[str, Item] = {}
        holds: dict[str, dict[str, Any]] = {}
        for raw in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            match entry.get("event"):
                case "hold":
                    holds[entry["pane_id"]] = entry
                case "release" | "hold_expired":
                    holds.pop(entry["pane_id"], None)
                case "item" if "item" in entry:
                    data = entry["item"]
                    data["state"] = State(data["state"])
                    items[entry["id"]] = Item(**data)
                case "item" if entry["id"] in items:
                    item = items[entry["id"]]
                    item.state = State(entry["state"])
                    item.reason = entry["reason"]
                    item.attempts = entry["attempts"]
                    item.updated = entry["t"]
        pending = sorted(
            (i for i in items.values() if i.state not in FINAL), key=lambda i: i.created
        )
        self._close_generation()
        now_wall = time.time()
        for pane_id, entry in holds.items():
            until_wall = entry.get("until_wall")
            if until_wall is not None and until_wall <= now_wall:
                continue
            hold = Hold(
                entry.get("owner", "unknown"),
                entry.get("reason", "held"),
                None if until_wall is None else self.clock() + (until_wall - now_wall),
                until_wall,
                restored=True,
            )
            self._journal_hold(pane_id, hold)
            self.holds[pane_id] = hold
        for item in pending:
            item.retry_at = 0.0
            item.updated = time.time()
            self.items[item.id] = item
            if item.state in IN_FLIGHT:
                # The write may have happened before the restart and was not confirmed:
                # delivery is unknown, and writing again could duplicate it.
                item.reason = f"dispatcher restarted while {item.state}; delivery unknown"
                item.state = State.FAILED
            else:
                item.state = State.QUEUED
                item.reason = "restored after restart"
                self.queues.setdefault(item.pane_id, []).append(item.id)
            self._record(item, item.reason, full=True)
