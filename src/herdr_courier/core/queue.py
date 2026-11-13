"""Per-pane FIFO queues, the delivery state machine, and the journal.

One worker thread calls ``tick``; it is the only writer to any pane, which is what makes the
per-pane queue a lock. Request threads only enqueue, hold, release, cancel and read status.
"""

import hashlib
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from .. import defaults, ledger, mailbox, probes, routes
from .gate import Observation, check
from .inputline import InputLine, LineState, classify, holds, matches, rejection
from .transport import Herdr, HerdrError, same_socket

VERIFY_SECONDS = 5.0
MAX_ATTEMPTS = 5
KEEP_FINISHED = 200
# Each restart closes one journal generation; the older ones are the only delivery history.
JOURNAL_KEEP = 10
# herdr rejects these before writing any byte, so a retry cannot duplicate a message.
SUBMIT_RETRYABLE = frozenset({"agent_blocked", "agent_not_ready"})
TARGET_GONE = frozenset({"agent_not_found", "pane_not_found"})
# How long a message waits for an agent that lost its session or, for the agents in
# ``hold_missing``, for the agent to come back at all.
HOLD_TTL = 4 * 3600.0
SUMMARY_EVERY = 15 * 60.0
# A returning agent gets this many held messages typed in, then one line about the rest, so a
# long outage does not bury it in a backlog.
RELEASE_AT_ONCE = 10
LOOKUP_EVERY = 5.0
# Starts the reason of a message whose route was not taken, so courier send can warn about it.
ROUTE_MISSED = "not routed: "
# How often filed messages are looked up in their readers' mailboxes, and how long one may stay
# unshown before its pointer line is typed again, at most REWAKES times.
MAILBOX_EVERY = 2.0
REWAKE_SECONDS = 300.0
REWAKES = 3
# A reader seen busy and then idle this long with the message still unshown had no Stop hook to
# bring it: the pointer line goes at once instead of after REWAKE_SECONDS.
IDLE_WAKE_SECONDS = 10.0
# A ghost whose text is still in the box gets Enter pressed again this often, at most this many
# times, whenever the pane is idle, unfocused (a reader's pointer: focused too) and still holds
# exactly that text.
GHOST_ENTER_SECONDS = 60.0
GHOST_ENTERS = 3
FOCUSED_READER_GRACE = 30.0


class State(StrEnum):
    QUEUED = "queued"
    WAITING = "waiting"
    SUBMITTING = "submitting"
    VERIFYING = "verifying"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    GHOST = "ghost"
    # In a mailbox reader's mailbox, not yet shown by its hook: sent once the hook shows it.
    FILED = "filed"


FINAL = frozenset({State.SENT, State.FAILED, State.CANCELLED, State.REJECTED, State.GHOST})
# From the moment the write may have started; such an item is never written again.
IN_FLIGHT = frozenset({State.SUBMITTING, State.VERIFYING})
# Ends of a typed pointer line that leave its message in the mailbox, for the hook to bring.
POINTER_ENDS = frozenset({State.SENT, State.GHOST, State.FAILED, State.REJECTED})


@dataclass(kw_only=True)
class Message:
    id: str
    target: str
    text: str
    source: str
    human: bool
    created: float
    client_id: str | None = None


@dataclass(kw_only=True)
class Delivery:
    pane_id: str
    session: str | None
    state: State = State.QUEUED
    reason: str = ""
    attempts: int = 0
    updated: float = 0.0
    retry_at: float = 0.0
    verify_until: float = 0.0
    terminal: str | None = None
    # Wall time after which a held item fails. An item held for a missing agent has no pane
    # (pane_id "") until the agent is back.
    held_until: float | None = None
    # The name the sender used when a route sent the item to ``target`` instead.
    routed_from: str | None = None


@dataclass
class Item(Message, Delivery):
    def as_dict(self) -> dict:
        # Journal and API field order predates the message/delivery split.
        values = asdict(self)
        return {
            key: values[key]
            for key in (
                "id",
                "target",
                "pane_id",
                "session",
                "text",
                "source",
                "human",
                "created",
                "state",
                "reason",
                "attempts",
                "updated",
                "retry_at",
                "verify_until",
                "client_id",
                "terminal",
                "held_until",
                "routed_from",
            )
        }


@dataclass
class Missing:
    """Messages held for an agent in ``hold_missing`` while herdr does not know it."""

    since: float
    items: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    back: str | None = None
    released: int = 0
    checked: float = -math.inf
    summary_at: float | None = None
    summary_said: tuple | None = None

    def reason(self, name: str) -> str:
        return f"hold: {name} missing since {_hhmm(self.since)}"


def _hhmm(wall: float) -> str:
    return time.strftime("%H:%M", time.localtime(wall))


def _command(text: str) -> bool:
    """A slash command such as /compact, which the agent reads only as the whole prompt."""
    return text.lstrip().startswith("/")


def _framed(item: "Item") -> str:
    """``item`` as one part of a merged prompt, with the header line `courier inbox` uses."""
    return f"--- {item.id} from {item.source} at {_hhmm(item.created)} ---\n{item.text}"


def pointer(item: "Item") -> str:
    """The one line typed for a mailbox reader; its hook puts the text into the context."""
    return (
        f"[courier] {item.id} from {item.source}: in your mailbox, courier inbox --show {item.id}"
    )


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
    state: tuple[str, int | None] | None = None
    state_since: float | None = None
    last: str = ""


class DispatchError(Exception):
    pass


class LedgerCapacity(DispatchError):
    pass


class WrongServer(Exception):
    """The journal holds work for another herdr server than the one this process reaches."""


class Dispatcher:
    def __init__(
        self,
        herdr: Herdr,
        journal: Path,
        *,
        clock=time.monotonic,
        log=print,
        settle_for: Callable[[str | None], float | None] | None = None,
        server: str | None = None,
        hold_missing: Iterable[str] = (),
        hold_ttl: float = HOLD_TTL,
        summary_to: dict[str, str] | None = None,
        summary_every: float = SUMMARY_EVERY,
        wall=time.time,
        controller: str = defaults.LEDGER_CONTROLLER,
        secretary: str = defaults.LEDGER_NOTIFY_TO,
        notify_to: str | None = None,
        notify_on_override: bool = defaults.LEDGER_NOTIFY_ON_OVERRIDE,
        notify_on_capacity: bool = defaults.LEDGER_NOTIFY_ON_CAPACITY,
        check_execution: bool = defaults.LEDGER_CHECK_EXECUTION,
        membership_field: str = defaults.LEDGER_MEMBERSHIP_FIELD,
        routes_in: Path | None = None,
        merge_chars: int = defaults.DELIVERY_MERGE_CHARS,
        pointer_for: Iterable[str] = (),
    ) -> None:
        """``settle_for`` maps an agent name to its between-turns settle time, or None (gate).

        ``server`` is the herdr socket every call of ``herdr`` reaches; the journal records it
        so that queued work is never replayed against another server's pane ids.

        A message for an agent in ``hold_missing`` that herdr does not know, or that lost its
        session, is held for ``hold_ttl`` seconds instead of failing, and the agent named for it
        in ``summary_to`` is told how many are held. ``wall`` is the clock of those deadlines,
        which survive a restart.

        ``routes_in`` is the state directory whose ``routes.json`` keeps the routes of
        ``set_route`` (``courier route``) across a restart; it is read only at the start, so a
        route changes only through a request this process can check. A route is taken when a
        message is queued, and again for every undelivered message when it changes; a message
        whose route target is missing or gone goes to the name it was sent to, so turning a
        route on or off never drops one.

        When a pane is ready and more messages wait for it, the next ones are typed in with the
        first as one prompt, each under its own header line, while the prompt stays within
        ``merge_chars``; 0 types each alone. They end in the first one's state.

        A message for an agent in ``pointer_for`` (mailbox readers, whose hook shows the text from
        the mailbox) is typed as one ``pointer`` line, so the text reaches the agent once; a slash
        command is still typed whole."""
        self.herdr = herdr
        self.journal = journal
        self.clock = clock
        self.wall = wall
        self.controller = controller
        self.secretary = secretary
        self._notify_to = notify_to
        self.notify_on_override = notify_on_override
        self.notify_on_capacity = notify_on_capacity
        self.check_execution = check_execution
        self.membership_field = membership_field
        self.log = log
        self.settle_for = settle_for
        self.server = server
        self.hold_missing = frozenset(hold_missing)
        self.hold_ttl = hold_ttl
        self.summary_to = dict(summary_to or {})
        self.summary_every = summary_every
        self.lock = threading.Condition()
        self.items: dict[str, Item] = {}
        self._enter_retried: set[str] = set()
        self._submit_requests: dict[str, Future[Item]] = {}
        self._rejections_before: dict[str, str | None] = {}
        self.merge_chars = merge_chars
        self.pointer_for = frozenset(pointer_for)
        # Courier's own notices to a reader that could not be filed: typed in full instead.
        self._unfiled: set[str] = set()
        # Filed items: when they were filed or last woken, and how often they were woken again.
        self._filed_at: dict[str, float] = {}
        self._rewakes: dict[str, int] = {}
        # Filed items no pointer line was typed for yet; of those, the ones whose reader was
        # seen busy, and since when it has been idle again.
        self._unpointed: set[str] = set()
        self._reader_busy: set[str] = set()
        self._idle_since: dict[str, float] = {}
        # Filed items left to the reader's `courier inbox --wait`, woken when it ends without them.
        self._for_waiter: set[str] = set()
        # Ghosts that got Enter again: how often, and when last.
        self._ghost_enters: dict[str, tuple[int, float]] = {}
        self._mailbox_checked = -math.inf
        # For an item typed in with others: the merged prompt, and the ids that rode with it.
        self._carried: dict[str, str] = {}
        self._riders: dict[str, list[str]] = {}
        # Ghosts not yet reported: their text may still sit in a box a person types into.
        self._ghosts: list[str] = []
        self._ghosted: set[str] = set()
        self.queues: dict[str, list[str]] = {}
        self.holds: dict[str, Hold] = {}
        self.watch: dict[str, PaneWatch] = {}
        self.missing: dict[str, Missing] = {}
        # The session each agent in hold_missing last had, for messages queued while it is gone.
        self.seen: dict[str, str | None] = {}
        self.tasks: dict[str, dict] = {}
        self.ledger_notices: dict[str, dict] = {}
        self._last_compact_size = 0
        self.routes_in = routes_in
        self.routes: dict[str, str] = {}
        self._routes_changed = False
        self._fallback: set[str] = set()
        self._load_routes()
        self._replay()
        self._pending_tasks()
        if server:
            self._journal({"event": "server", "socket": server})

    def enqueue(
        self,
        target: str,
        text: str,
        *,
        source: str,
        human: bool = False,
        client_id: str | None = None,
        hold: bool = True,
        task: dict | None = None,
        item_id: str | None = None,
        filed: bool = False,
    ) -> Item:
        """Queue ``text`` for ``target``. ``hold=False`` refuses instead of holding it for a
        missing agent, so courier's own summaries never wait on one another. ``item_id`` is one
        the client chose, so files it copied for the message sit under the message's id.
        ``filed`` says the caller copies the item to the mailbox itself (a client's message, with
        its attachments); anything else for a mailbox reader is filed here before it is typed."""
        if not text.strip():
            raise DispatchError("empty message")
        if text.rstrip().endswith("\\"):
            # Claude Code reads a backslash before Enter as a line break: the message would stay
            # in the box unsent, where the next person typing there would send it with theirs.
            raise DispatchError(
                "message ends with a backslash, which the agent's input box takes as a line "
                "break instead of sending; end it with another character"
            )
        if client_id and (known := self._by_client_id(client_id)):
            return known
        if item_id is not None and item_id in self.items:
            raise DispatchError(f"id {item_id} is in use")
        asked, note = target, ""
        if (routed := self._route(target)) != target:
            if (agent := self._live(routed)) is not None:
                target = routed
            else:
                note = f"{ROUTE_MISSED}{asked} → {routed}, but {routed} is not running"
                self.log(f"courier: {note}; queued for {asked}")
        if target == asked:
            try:
                agent = self.herdr.agent_get(target)
            except HerdrError as exc:
                if hold and target in self.hold_missing and exc.code in TARGET_GONE:
                    held = self._enqueue_held(
                        target, text, source, human, client_id, task=task, item_id=item_id
                    )
                    if not filed:
                        self._file(held)
                    return replace(held, reason=f"{note}; {held.reason}") if note else held
                raise DispatchError(f"cannot resolve target {target!r}: {exc}") from exc
        session = (agent.get("agent_session") or {}).get("value")
        if target in self.hold_missing:
            with self.lock:
                self.seen[target] = session
        item = Item(
            id=item_id or uuid.uuid4().hex[:10],
            target=target,
            pane_id=agent["pane_id"],
            session=session,
            text=text,
            source=source,
            human=human,
            created=time.time(),
            reason=note,
            client_id=client_id,
            terminal=agent.get("terminal_id"),
            routed_from=asked if target != asked else None,
        )
        with self.lock:
            # A retried request may have raced the first one while the target was resolved.
            if client_id and (known := self._by_client_id(client_id)):
                return known
            if task is not None:
                task["dispatch_id"] = item.id
            if not filed:
                self._file(item)
            self._record(item, "queued", full=True, task=task)
            self.items[item.id] = item
            if task is not None:
                self.tasks[task["key"]] = task
            self._push(item)
            self.lock.notify_all()
        return item

    def _file(self, item: Item) -> None:
        """Copy an item for a mailbox reader to its mailbox, so the pointer line typed for it
        leads to the text; one that cannot be copied is typed in full instead."""
        if not self._pointed(item):
            return
        try:
            mailbox.add(self.journal.parent, item)
        except OSError as exc:
            self._unfiled.add(item.id)
            self.log(f"courier: {item.id} for {item.target} not filed, typed in full: {exc}")

    def _push(self, item: Item, *, in_order: bool = False) -> None:
        """Queue ``item`` on its pane; ``in_order`` places a moved item among the waiting ones
        by the time it was sent, never ahead of one in flight or of a human message."""
        queue = self.queues.setdefault(item.pane_id, [])
        if in_order:
            pos = next(
                (
                    n
                    for n, i in enumerate(queue)
                    if self.items[i].state not in IN_FLIGHT
                    and (item.human or not self.items[i].human)
                    and self.items[i].created > item.created
                ),
                len(queue),
            )
            queue.insert(pos, item.id)
        elif item.human:
            # Human messages go ahead of queued agent traffic but behind an in-flight one.
            pos = sum(1 for i in queue if self.items[i].human or self.items[i].state in IN_FLIGHT)
            queue.insert(pos, item.id)
        else:
            queue.append(item.id)

    def send_task(
        self,
        target: str,
        text: str,
        *,
        source: str,
        client_id=None,
        human=False,
        task=None,
        reopen=False,
        item_id=None,
    ) -> Item:
        # Condition uses an RLock: enqueue and journal helpers reacquire it below.
        with self.lock:
            if client_id and (known := self._by_client_id(client_id)):
                return known
            if task is not None:
                try:
                    task = ledger.new(
                        task["key"],
                        source,
                        target,
                        task["evidence"],
                        **{k: task[k] for k in ledger.FIELDS if k in task},
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise DispatchError(str(exc)) from exc
                old = self.tasks.get(task["key"])
                if old is not None:
                    if old["state"] in ledger.ACTIVE:
                        if old["to"] != target:
                            raise DispatchError(f"owned by {old['to']} since {_hhmm(old['sent'])}")
                        return self._task_reminder(old, source, client_id)
                    if not reopen:
                        raise DispatchError("closed key requires --reopen")
                    task["attempts"] = old["attempts"] + 1
                self._task_capacity(task["key"])
                for other in self.tasks.values():
                    if (
                        other["key"] != task["key"]
                        and other["state"] in ledger.ACTIVE
                        and task.get("repo")
                        and task.get("branch")
                        and (other["to"], other.get("repo"), other.get("branch"))
                        == (target, task["repo"], task["branch"])
                    ):
                        raise DispatchError(f"open as {other['key']}")
            item = self.enqueue(
                target,
                text,
                source=source,
                human=human,
                client_id=client_id,
                task=task,
                item_id=item_id,
                filed=True,
            )
            self._task_report(source, text)
            return item

    def _task_reminder(self, task: dict, source: str, client_id: str | None) -> Item:
        now = ledger.time.time()
        if now - task.get("reminded_at", -math.inf) >= 1800:
            item = self.enqueue(
                task["to"],
                f"REMINDER {task['key']}, ignore if running",
                source=source,
                client_id=client_id,
                filed=True,
            )
            task["reminded_at"] = now
        else:
            item = Item(
                id=uuid.uuid4().hex[:10],
                target=task["to"],
                pane_id="",
                session=None,
                text="",
                source=source,
                human=False,
                created=now,
                state=State.SENT,
                reason="reminder suppressed within 30 minutes",
                client_id=client_id,
            )
            self._record(item, item.reason, full=True)
            self.items[item.id] = item
            task["reminders_suppressed"] = task.get("reminders_suppressed", 0) + 1
        self._task_record(task)
        return item

    def _controller_pane(self, sender_pane: str) -> None:
        try:
            sender = self.herdr.agent_get(sender_pane)
        except HerdrError as exc:
            raise DispatchError("cannot identify sender pane") from exc
        # A deputy the controller's name is routed to acts for it.
        controllers = {self.controller, self._route(self.controller)}
        if sender.get("pane_id") != sender_pane or sender.get("name") not in controllers:
            raise DispatchError("reassign requires controller pane identity")

    def cancel_task(self, key: str, *, sender_pane: str) -> dict:
        with self.lock:
            self._controller_pane(sender_pane)
            task = self.tasks.get(key)
            if task is None:
                raise DispatchError("no task")
            if task["state"] not in ledger.ACTIVE:
                return dict(task)
            item = self.items.get(task.get("dispatch_id"))
            if item is not None and item.state not in FINAL and item.state not in IN_FLIGHT:
                self._finish(item, State.CANCELLED, "task cancelled before delivery")
            task.update(state="CANCELLED", ended=ledger.time.time(), cancelled_by=sender_pane)
            self._task_record(task)
            return dict(task)

    def reassign_task(
        self, key: str, target: str, *, sender_pane: str, force=False, reason=""
    ) -> dict:
        with self.lock:
            self._controller_pane(sender_pane)
            if force and not reason.strip():
                raise DispatchError("--force requires a reason")
            task = self.tasks.get(key)
            if task is None or task["state"] not in ledger.ACTIVE:
                raise DispatchError("no active task")
            if task.get("secretary_notice"):
                raise DispatchError("previous override notification is pending")
            try:
                agent = self.herdr.agent_get(task["to"])
            except HerdrError as exc:
                if exc.code not in TARGET_GONE:
                    raise DispatchError(f"cannot check old owner: {exc}") from exc
                agent = {"agent_status": "missing"}
            reasons = []
            if (
                self.check_execution
                and not {"lane_state_task", "descendants_working"} <= agent.keys()
            ):
                reasons.append("execution proof unavailable")
            if agent.get("agent_status") == "working" or (
                self.check_execution and agent.get("descendants_working")
            ):
                reasons.append(f"{task['to']} working")
            elif agent.get("agent_status") not in {"idle", "missing"}:
                reasons.append(f"{task['to']} execution state uncertain")
            if self.check_execution and agent.get("lane_state_task") == key:
                reasons.append(f"{task['to']} lane-state still owns {key}")
            try:
                if ledger.evidence_exists(task):
                    reasons.append("evidence exists: verify first")
            except (OSError, ValueError) as exc:
                reasons.append(str(exc))
            item = self.items.get(task.get("dispatch_id"))
            if item is not None and item.state in IN_FLIGHT:
                reasons.append("delivery in flight")
            if reasons and not force:
                raise DispatchError("; ".join(reasons))
            self.resolve(target)
            for other in self.tasks.values():
                if (
                    other["key"] != key
                    and other["state"] in ledger.ACTIVE
                    and task.get("repo")
                    and task.get("branch")
                    and (other["to"], other.get("repo"), other.get("branch"))
                    == (target, task["repo"], task["branch"])
                ):
                    raise DispatchError(f"open as {other['key']}")
            if item is not None and item.state not in FINAL and item.state not in IN_FLIGHT:
                self._finish(item, State.CANCELLED, "task reassigned before delivery")
            task.setdefault("previous_owners", []).append(task["to"])
            task["previous_owners"] = task["previous_owners"][-20:]
            task["to"] = target
            task["attempts"] += 1
            task["reassigned_by"] = sender_pane
            if force:
                task["override"] = reasons or [f"{self.controller} --force"]
                task["override_reason"] = reason
                if self.notify_on_override:
                    task["secretary_notice"] = (
                        f"FORCE reassign {key} to {target} by {sender_pane}: {reason}; "
                        + "; ".join(reasons)
                    )
            self._task_record(task)
            self._flush_task_notices()
            return dict(task)

    def _task_record(self, task: dict) -> None:
        self._journal({"event": "task", "task": task})

    def _capacity_notice(self, name: str, text: str) -> None:
        if not self.notify_on_capacity:
            return
        if name not in self.ledger_notices:
            notice = {"name": name, "text": text, "sent": False}
            self.ledger_notices[name] = notice
            self._journal({"event": "ledger-notice", **notice})
        self._flush_task_notices()

    def flag_task(self, key: str, reason: str) -> dict:
        with self.lock:
            task = self.tasks.get(key)
            if task is None or task["state"] not in {"OPEN", "VERIFYING"}:
                raise DispatchError("no open or verifying task")
            day = int(ledger.time.time() // 86400)
            digest = hashlib.sha256(reason.encode()).hexdigest()
            flags = task.setdefault("probe_flags", {})
            if flags.get(digest) == day:
                return dict(task)
            try:
                self.enqueue(task["from"], f"VERIFY {key}: {reason}", source="courier", hold=False)
            except DispatchError as exc:
                self.log(f"probe flag not queued: {exc}")
                return dict(task)
            flags[digest] = day
            self._task_record(task)
            return dict(task)

    def close_task(self, key: str, *, by: str, evidence: str, max_probes=100) -> dict:
        with self.lock:
            task = self.tasks.get(key)
            if task is None:
                raise DispatchError("no task")
            if by != "probe":
                raise DispatchError("close requires --by probe")
            if task["state"] == "DONE":
                return dict(task)
            if task["state"] not in {"OPEN", "VERIFYING"}:
                raise DispatchError("task is not open or verifying")
            snapshot = deepcopy(task)
        result = probes.check(snapshot, max_probes=max_probes)
        with self.lock:
            task = self.tasks.get(key)
            fields = ("attempts", "to", "state", "sent", "evidence", "cwd")
            if task is None or any(task.get(field) != snapshot.get(field) for field in fields):
                raise DispatchError("task changed during probe; defer until next verification pass")
            if not result["ok"]:
                self.flag_task(key, result["reason"])
                raise DispatchError(result["reason"])
            if task["state"] == "OPEN" and not probes.log_report(result):
                self.flag_task(key, "artifact found, no report")
                raise DispatchError("artifact found, no report")
            task = deepcopy(task)
            task["state"] = "DONE"
            task["ended"] = ledger.time.time()
            kinds = list(dict.fromkeys(item["kind"] for item in result["results"]))
            task["closed_by"] = "probe " + ",".join(kinds)
            task["probe_results"] = result["results"]
            task["close_note"] = evidence
            self._task_record(task)
            self.tasks[key] = task
            return dict(task)

    def _task_capacity(self, key: str) -> None:
        if key in self.tasks and "sha" not in self.tasks[key]:
            return
        details = [t for t in self.tasks.values() if "sha" not in t]
        tombstones = sum("sha" in t for t in self.tasks.values())
        terminal = sorted(
            (
                t
                for t in details
                if t["state"] not in ledger.ACTIVE
                and t["key"] != key
                and not t.get("secretary_notice")
            ),
            key=lambda t: t.get("ended", t["sent"]),
        )
        while (
            len(details) >= ledger.DETAIL_LIMIT and terminal and tombstones < ledger.TOMBSTONE_LIMIT
        ):
            old = terminal.pop(0)
            compact = {
                "key": old["key"],
                "state": old["state"],
                "ended": old.get("ended", old["sent"]),
                "sha": hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest(),
                "attempts": old["attempts"],
            }
            self.tasks[old["key"]] = compact
            self._task_record(compact)
            details.remove(old)
            tombstones += 1
        reopening = key in self.tasks
        if len(details) >= ledger.DETAIL_LIMIT or (
            tombstones >= ledger.TOMBSTONE_LIMIT and not reopening
        ):
            self._capacity_notice(
                "capacity-hard",
                f"LEDGER CAPACITY: refused {key}; "
                "archive or review retained task records before dispatching new keys",
            )
            raise LedgerCapacity("ledger_capacity: task retention hard cap reached")
        if len(details) + 1 >= math.ceil(0.8 * ledger.DETAIL_LIMIT) or tombstones >= math.ceil(
            0.8 * ledger.TOMBSTONE_LIMIT
        ):
            self._capacity_notice(
                "capacity-soft",
                "LEDGER CAPACITY: 80% retention threshold; review task history before the hard cap",
            )

    @property
    def notify_to(self) -> str:
        return self.secretary if self._notify_to is None else self._notify_to

    def _notify(self, text: str, client_id: str) -> None:
        item = Item(
            id=hashlib.sha256(client_id.encode()).hexdigest()[:10],
            target=self.notify_to,
            pane_id="",
            session=None,
            text=text,
            source="courier",
            human=False,
            created=ledger.time.time(),
        )
        if mailbox.add(self.journal.parent, item):
            try:
                self.enqueue(
                    self.notify_to, text, source="courier", client_id=client_id, item_id=item.id
                )
            except DispatchError as exc:
                self.log(f"{self.notify_to} notice kept in mailbox: {exc}")

    def _flush_task_notices(self) -> None:
        for notice in self.ledger_notices.values():
            if not notice["sent"] and self.notify_on_capacity:
                try:
                    self._notify(notice["text"], "ledger:" + notice["name"])
                except OSError as exc:
                    self.log(f"{self.notify_to} notification pending: {exc}")
                    continue
                notice["sent"] = True
                self._journal({"event": "ledger-notice", **notice})
        for task in self.tasks.values():
            if (text := task.get("secretary_notice")) and self.notify_on_override:
                client_id = f"force:{task['key']}:{task['attempts']}"
                try:
                    self._notify(text, client_id)
                except OSError as exc:
                    self.log(f"override notification pending: {exc}")
                    continue
                task.pop("secretary_notice")
                task["secretary_notified"] = True
                self._task_record(task)

    def _compact_task_journal(self) -> None:
        if (
            not self.tasks
            or not self.journal.exists()
            or self.journal.stat().st_size < self._last_compact_size + ledger.JOURNAL_BYTES
        ):
            return
        self._close_generation()
        if self.server:
            self._journal({"event": "server", "socket": self.server})
        for pane_id, hold in self.holds.items():
            self._journal_hold(pane_id, hold)
        for task in self.tasks.values():
            self._task_record(task)
        for notice in self.ledger_notices.values():
            self._journal({"event": "ledger-notice", **notice})
        for item in self.items.values():
            self._record(item, item.reason, full=True)
        self._last_compact_size = self.journal.stat().st_size

    def _task_report(self, source: str, text: str) -> None:
        report = re.search(r"(?:^|\s)(DONE|READY|BLOCKED|FAILED)\s+(\S+)", text)
        if not report or (task := self.tasks.get(report[2])) is None:
            return
        try:
            source = self.herdr.agent_get(source).get("name") or source
        except HerdrError:
            pass
        if source != task["to"]:
            self.log(f"ignored {report[1]} {report[2]} from non-owner {source}")
            return
        if task["state"] not in {"OPEN", "VERIFYING"}:
            return
        state = "VERIFYING" if report[1] in {"DONE", "READY"} else report[1]
        if state == task["state"]:
            return
        task["state"] = state
        if state not in ledger.ACTIVE:
            task["ended"] = ledger.time.time()
        self._task_record(task)
        if state in {"FAILED", "BLOCKED"}:
            self._task_flag(task, state, f"{state} {task['key']}: {text}")

    def _task_flag(self, task: dict, reason: str, text: str) -> None:
        if reason in task["flags"]:
            return
        try:
            self.enqueue(task["from"], text, source="courier", hold=False)
        except DispatchError as exc:
            self.log(f"task flag not queued: {exc}")
            return
        task["flags"].append(reason)
        self._task_record(task)

    def check_tasks(self) -> None:
        with self.lock:
            now = ledger.time.time()
            names = ledger.role_names(self.membership_field)
            for task in self.tasks.values():
                if task["state"] not in {"OPEN", "VERIFYING"}:
                    continue
                due = task.get("due")
                if due is not None:
                    for n in (1, 2):
                        if now >= task["sent"] + n * (due - task["sent"]):
                            self._task_flag(task, f"overdue-{n}", f"OVERDUE {task['key']} {n}x")
                if now - task["sent"] > 7 * 86400 or (
                    names is not None and task["to"] not in names
                ):
                    self._task_flag(task, "stale", f"STALE {task['key']}")

    def _pending_tasks(self) -> None:
        path = self.journal.parent / "pending-dispatch.jsonl"
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                task = json.loads(raw)
                task = ledger.new(
                    task["key"],
                    task["from"],
                    task["to"],
                    task["evidence"],
                    **{k: task[k] for k in ledger.FIELDS if k in task},
                )
            except (ValueError, KeyError, TypeError):
                continue
            if task["key"] not in self.tasks:
                try:
                    self._task_capacity(task["key"])
                except LedgerCapacity:
                    self.log("pending dispatch retained: ledger_capacity")
                    return
                task["note"] = "manual"
                self.tasks[task["key"]] = task
                self._task_record(task)
        path.rename(path.with_name(f"pending-dispatch.{uuid.uuid4().hex}.replayed.jsonl"))

    def _enqueue_held(
        self,
        target: str,
        text: str,
        source: str,
        human: bool,
        client_id: str | None,
        *,
        task: dict | None = None,
        item_id: str | None = None,
    ) -> Item:
        with self.lock:
            if client_id and (known := self._by_client_id(client_id)):
                return known
            missing = self._missing(target)
            item = Item(
                id=item_id or uuid.uuid4().hex[:10],
                target=target,
                pane_id="",
                session=self.seen.get(target),
                text=text,
                source=source,
                human=human,
                created=time.time(),
                state=State.WAITING,
                reason=missing.reason(target),
                client_id=client_id,
                held_until=self.wall() + self.hold_ttl,
            )
            if task is not None:
                task["dispatch_id"] = item.id
            self._record(item, item.reason, full=True, task=task)
            self.items[item.id] = item
            if task is not None:
                self.tasks[task["key"]] = task
            missing.items.append(item.id)
            self.lock.notify_all()
        return item

    def _missing(self, name: str) -> Missing:
        if (missing := self.missing.get(name)) is None or missing.back is not None:
            # Gone again after coming back: a new outage, but the older items stay with it.
            old = missing
            missing = self.missing[name] = Missing(since=self.wall())
            if old is not None:
                missing.items, missing.expired = old.items, old.expired
        return missing

    def _hold_by_name(self, item: Item, **changed: Any) -> None:
        """Take a queued item off its pane and hold it until its agent is back (lock held)."""
        queue = self.queues.get(item.pane_id, [])
        if item.id in queue:
            queue.remove(item.id)
        missing = self._missing(item.target)
        if item.held_until is None:
            item.held_until = self.wall() + self.hold_ttl
        item.pane_id = ""
        if item.id not in missing.items:
            missing.items.append(item.id)
            missing.items.sort(key=lambda i: self.items[i].created)
        self._set(
            item,
            State.WAITING,
            missing.reason(item.target),
            pane_id="",
            held_until=item.held_until,
            **changed,
        )

    def _refused(self, item: Item, verdict) -> bool:
        """Fail or hold an item the gate refused for its identity; True if it did (lock held).

        A missing session may be the same agent restarting (a resume), so the item waits for it
        until ``hold_ttl``; another session fails it, since it is another agent. For an agent in
        ``hold_missing`` both wait, by name, and only a return with the same session sends it.
        """
        if not (verdict.fatal or verdict.lost):
            return False
        if item.routed_from:
            self._route_back(item, f"{item.target}: {verdict.reason}")
            return True
        if item.target in self.hold_missing:
            self._hold_by_name(item)
        elif verdict.fatal:
            self._finish(item, State.FAILED, verdict.reason)
        else:
            now = self.wall()
            if item.held_until is None:
                item.held_until = now + self.hold_ttl
            if now >= item.held_until:
                self._finish(item, State.FAILED, f"hold expired: {verdict.reason}")
            else:
                self._set(
                    item, State.WAITING, f"hold: {verdict.reason}", held_until=item.held_until
                )
        return True

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
                if item.state in FINAL or item.state is State.FILED or left <= 0:
                    return item
                self.lock.wait(min(left, 1.0))

    def status(self, pane: str | None = None) -> dict[str, Any]:
        with self.lock:
            now = self.clock()
            panes = {}
            wanted = sorted(set(self.queues) | set(self.holds)) if pane is None else [pane]
            for pane_id in wanted:
                hold = self.holds.get(pane_id)
                pending = [self.items[i].as_dict() for i in self.queues.get(pane_id, [])]
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
            held = {
                name: {
                    "since": m.since,
                    "back": m.back,
                    "pending": [self.items[i].as_dict() for i in m.items],
                }
                for name, m in self.missing.items()
                if pane is None and m.items
            }
            filed = [
                i.as_dict()
                for i in self.items.values()
                if i.state is State.FILED and (pane is None or i.pane_id == pane)
            ]
            return {
                "panes": panes,
                "held": held,
                "filed": filed,
                "recent": [i.as_dict() for i in finished],
                "routes": dict(self.routes),
            }

    def tick(self) -> None:
        self._submit_owned_requests()
        if self.tasks or self.ledger_notices:
            with self.lock:
                self._flush_task_notices()
                self._compact_task_journal()
        if self.tasks:
            self.check_tasks()
        with self.lock:
            now = self.clock()
            for pane_id, hold in list(self.holds.items()):
                if hold.until is not None and hold.until <= now:
                    self._journal({"event": "hold_expired", "pane_id": pane_id})
                    del self.holds[pane_id]
        self._apply_routes()
        if self.missing:
            self._look_for_missing()
        if self._filed_at and self.clock() - self._mailbox_checked >= MAILBOX_EVERY:
            self._check_filed()
        with self.lock:
            heads = [
                (pane_id, self.items[queue[0]]) for pane_id, queue in self.queues.items() if queue
            ]
        for pane_id, item in heads:
            try:
                self._advance(pane_id, item)
            except Exception as exc:  # noqa: BLE001 - one bad pane must not stop the others
                with self.lock:
                    self._stall(item, f"dispatcher error: {exc!r}")
        if self._ghosts:
            self._report_ghosts()
        if self._ghosted:
            self._enter_ghosts()

    def _enter_ghosts(self) -> None:
        """Press Enter again on courier's own text left in a box, so an idle agent is not left
        with it until someone looks. The same guards as the first retry apply: exactly the
        message in the box, an idle agent with the same session, no hold, and no focus unless
        the text is a reader's pointer line."""
        now = self.clock()
        with self.lock:
            due = [
                self.items[i]
                for i in self._ghosted
                if i in self.items
                and self.items[i].state in (State.GHOST, State.FILED)
                and self.items[i].pane_id
                and self._ghost_enters.get(i, (0, -math.inf))[0] < GHOST_ENTERS
                and now - self._ghost_enters.get(i, (0, -math.inf))[1] >= GHOST_ENTER_SECONDS
            ]
        for item in due:
            count = self._ghost_enters.get(item.id, (0, 0.0))[0] + 1
            self._ghost_enters[item.id] = (count, now)
            try:
                self._retry_enter(item, manual=True)
            except DispatchError as exc:
                self.log(f"courier: {item.id} left in {item.pane_id}, Enter not pressed: {exc}")
                continue
            self.log(f"courier: pressed Enter again on {item.id} left in {item.pane_id}")

    def _reader_state(self, item: Item) -> str | None:
        """The mailbox state (new, read, done) of an item typed as a pointer line, or None."""
        if not self._pointed(item):
            return None
        try:
            filed = mailbox.load(self.journal.parent, item.target)
        except OSError:
            return None
        return next((m.state for m in filed if m.id == item.id), None)

    def _await_reader(self, item: Item, reason: str) -> bool:
        """Leave ``item`` in its reader's mailbox for the hook to show (lock held). False when
        it is not a filed, unshown pointer message, which then goes on as typed text."""
        if self._reader_state(item) != "new":
            return False
        self._unqueue(item)
        self._set(item, State.FILED, reason)
        self._filed_at[item.id] = self.clock()
        self._unpointed.add(item.id)
        return True

    def _check_filed(self) -> None:
        """End each filed message its reader's hook has shown; type the pointer line again for
        one left unshown REWAKE_SECONDS, at most REWAKES times, and at once for one whose reader
        was busy and has been idle IDLE_WAKE_SECONDS without its Stop hook bringing it."""
        with self.lock:
            self._mailbox_checked = now = self.clock()
            filed = [self.items[i] for i in self._filed_at if i in self.items]
            stale = [i for i in self._filed_at if i not in self.items]
            for item_id in stale:
                self._forget_filed(item_id)
        states: dict[str, dict[str, str]] = {}
        for target in {i.target for i in filed}:
            try:
                states[target] = {m.id: m.state for m in mailbox.load(self.journal.parent, target)}
            except OSError:
                states[target] = {}
        wall = self.wall()
        live = {
            i.id
            for i in filed
            if i.id in self._for_waiter and mailbox.waiter_live(self.journal.parent, i.target, wall)
        }
        status: dict[str, str] = {}
        for pane_id in {i.pane_id for i in filed if i.pane_id}:
            try:
                status[pane_id] = self.herdr.agent_get(pane_id).get("agent_status", "unknown")
            except HerdrError:
                continue
        with self.lock:
            for item in filed:
                if item.state is not State.FILED:
                    self._forget_filed(item.id)
                    continue
                state = states[item.target].get(item.id)
                if state in ("read", "done"):
                    self._forget_filed(item.id)
                    self._rewakes.pop(item.id, None)
                    self._finish(item, State.SENT, f"shown to {item.target} by its hook")
                    continue
                if not item.pane_id:
                    continue
                if item.id in self._for_waiter and item.id not in live:
                    self._forget_filed(item.id)
                    self._wake(item, "its reader's waiting hook ended without it, pointer typed")
                elif self._reader_idle_long(item, status.get(item.pane_id), now):
                    self._forget_filed(item.id)
                    self._wake(item, "unshown after the reader went idle, pointer typed")
                elif (
                    now - self._filed_at[item.id] >= REWAKE_SECONDS
                    and self._rewakes.get(item.id, 0) < REWAKES
                ):
                    self._rewakes[item.id] = self._rewakes.get(item.id, 0) + 1
                    self._forget_filed(item.id)
                    self._wake(
                        item, f"unshown, pointer typed again ({self._rewakes[item.id]}/{REWAKES})"
                    )

    def _reader_idle_long(self, item: Item, status: str | None, now: float) -> bool:
        """Whether the reader of filed ``item`` was seen busy and has since been idle for
        IDLE_WAKE_SECONDS (lock held)."""
        if status in ("working", "blocked") and item.id in self._unpointed:
            self._reader_busy.add(item.id)
            self._idle_since.pop(item.id, None)
            return False
        if item.id not in self._unpointed:
            return False
        if status not in ("idle", "done") or item.id not in self._reader_busy:
            return False
        since = self._idle_since.setdefault(item.id, now)
        return now - since >= IDLE_WAKE_SECONDS

    def _forget_filed(self, item_id: str) -> None:
        self._for_waiter.discard(item_id)
        self._filed_at.pop(item_id, None)
        self._unpointed.discard(item_id)
        self._reader_busy.discard(item_id)
        self._idle_since.pop(item_id, None)

    def _wake(self, item: Item, reason: str) -> None:
        item.retry_at = 0.0
        self._set(item, State.QUEUED, reason)
        self._push(item, in_order=True)

    def _report_ghosts(self) -> None:
        """Tell someone other than the receiver that courier text was left in its input box.

        Anyone typing there next, also through another program, would send it along with their
        own words. Each ghost is reported once; a ghost that comes back after a restart is not."""
        with self.lock:
            ghosts = [self.items[i] for i in self._ghosts if i in self.items]
            self._ghosts.clear()
        for item in ghosts:
            names = {item.target, item.routed_from}
            to = next((n for n in (self.notify_to, self.controller) if n not in names), None)
            text = (
                f"courier: {item.id} for {item.target} ({item.pane_id}) was left unsent in its "
                f"input box ({item.reason}). Clear that box before anyone types there."
            )
            if to is None:
                self.log(text)
                continue
            try:
                self.enqueue(to, text, source="courier", hold=False)
            except DispatchError as exc:
                self.log(f"{text} Not queued for {to}: {exc}")

    def _look_for_missing(self) -> None:
        now = self.clock()
        with self.lock:
            due = [name for name, m in self.missing.items() if now >= m.checked + LOOKUP_EVERY]
            for name in due:
                self.missing[name].checked = now
        lines: list[tuple[str, str]] = []
        for name in due:
            try:
                agent = self.herdr.agent_get(name)
            except HerdrError:
                agent = None
            with self.lock:
                if (line := self._settle_missing(name, agent)) is not None:
                    lines.append((name, line))
        for name, line in lines:
            try:
                self.enqueue(name, line, source="courier", hold=False)
            except DispatchError as exc:
                self.log(f"courier: line for {name} not queued: {exc}")
        self._send_summaries()

    def _settle_missing(self, name: str, agent: dict | None) -> str | None:
        """Expire, release or keep the items held for ``name`` (lock held).

        Returns the line that tells a returning agent how many more are held, if any."""
        m = self.missing[name]
        wall = self.wall()
        for item_id in list(m.items):
            item = self.items.get(item_id)
            if item is None or item.state in FINAL:
                m.items.remove(item_id)
            elif item.held_until is not None and wall >= item.held_until:
                m.expired.append(item.id)
                self._finish(item, State.FAILED, f"hold expired: {m.reason(name)}")
        line = None
        # A pane without an agent session is still starting, or a bare shell: still missing.
        session = agent and (agent.get("agent_session") or {}).get("value")
        if session:
            self.seen[name] = session
            if m.back is None:
                m.back = session
                # Only a resume of the session the messages were queued for gets them typed in.
                ready = [i for i in m.items if self.items[i].session == session]
                for item_id in ready[:RELEASE_AT_ONCE]:
                    item = self.items[item_id]
                    m.items.remove(item_id)
                    item.pane_id, item.terminal = agent["pane_id"], agent.get("terminal_id")
                    item.held_until = None
                    self.queues.setdefault(item.pane_id, []).append(item_id)
                    self._set(
                        item,
                        State.QUEUED,
                        f"released: {name} is back",
                        pane_id=item.pane_id,
                        terminal=item.terminal,
                        held_until=None,
                    )
                    m.released += 1
                if m.released and m.items:
                    line = f"courier: {len(m.items)} more held for you: `courier inbox`"
        # Kept while a summary still has news for the other agent; _send_summaries drops it then.
        if not m.items and not m.expired and (m.summary_at is None or name not in self.summary_to):
            del self.missing[name]
        return line

    def _send_summaries(self) -> None:
        now = self.clock()
        due = []
        with self.lock:
            for name, m in self.missing.items():
                other = self.summary_to.get(name)
                if not other or (other in self.missing and self.missing[other].back is None):
                    continue
                said = (len(m.items), len(m.expired), m.back)
                if said == m.summary_said:
                    continue
                # The first summary goes at once, and so does the one that says the agent is back.
                came_back = m.back is not None and (m.summary_said or (0, 0, None))[2] is None
                if (
                    m.summary_at is not None
                    and not came_back
                    and now - m.summary_at < self.summary_every
                ):
                    continue
                due.append((name, other, said, self._summary(name, m)))
        for name, other, said, text in due:
            try:
                self.enqueue(other, text, source="courier", hold=False)
            except DispatchError:
                # The other agent is missing too: nothing to tell, and nothing may loop.
                continue
            with self.lock:
                if (m := self.missing.get(name)) is None:
                    continue
                m.summary_at, m.summary_said = now, said
                m.expired.clear()
                if m.back is not None and not m.items:
                    del self.missing[name]

    def _summary(self, name: str, m: Missing) -> str:
        held = [self.items[i] for i in m.items]
        parts = [f"courier: {name} missing since {_hhmm(m.since)}"]
        if held:
            sources: dict[str, int] = {}
            for item in held:
                sources[item.source] = sources.get(item.source, 0) + 1
            by = ", ".join(f"{s} {n}" for s, n in sources.items())
            parts.append(f"held {len(held)} ({by}); oldest {_hhmm(held[0].created)}")
        if m.expired:
            parts.append(f"{len(m.expired)} failed after {self.hold_ttl / 3600:g} h held")
        if m.back is not None:
            if m.released:
                parts.append(f"back with the same session; {m.released} typed in")
            elif held:
                parts.append(f"back with a new session; the held stay held: `courier inbox {name}`")
            else:
                parts.append("back")
        return "; ".join(parts)

    def _advance(self, pane_id: str, item: Item) -> None:
        now = self.clock()
        if item.retry_at > now:
            return
        obs = self._observe(item)
        if obs is None:
            return
        # Outside the lock: it may ask Ovis whether it is running.
        settle = None if self.settle_for is None else self.settle_for(obs.name)
        with self.lock:
            if item.state in FINAL:
                return
            verifying = item.state in IN_FLIGHT
            retry_enter = self._verify(item, obs, now) if verifying else False
        if verifying:
            if retry_enter:
                self._retry_enter(item)
            return
        with self.lock:
            if item.state in FINAL:
                return
            empty_for = self._empty_for(pane_id, obs.line, now)
            verdict = check(
                obs,
                session=item.session,
                hold=self._hold_reason(pane_id, now),
                empty_for=empty_for,
                settle=settle,
                settled_for=self._settled_for(pane_id, obs, now),
                terminal=item.terminal,
            )
            if self._refused(item, verdict):
                return
            # A reader with a live `courier inbox --wait` is woken by it; a pointer line too
            # would bring the message twice.
            if (
                self._pointed(item)
                and mailbox.waiter_live(self.journal.parent, item.target, self.wall())
                and self._await_reader(item, "in the mailbox for the reader's waiting hook")
            ):
                self._for_waiter.add(item.id)
                return
            # A reader that is working gets the message from its Stop hook, and one whose box
            # holds other text from the prompt-submit hook when that text is sent: neither
            # waits for the box, and the messages behind it do not wait either.
            if (
                not verdict.ok
                and (obs.status in ("working", "blocked") or obs.line.state is LineState.TYPED)
                and self._await_reader(item, "in the mailbox for the hook: " + verdict.reason)
            ):
                return
            if not verdict.ok:
                self._set(item, State.WAITING, verdict.reason)
                return
            # One pane is always focused, often an idle agent nobody watches, so focus cannot
            # stop the pointer line that wakes it. A longer empty box narrows the clash with
            # someone starting to type, which costs only the pointer: the hook brings the text.
            if (
                obs.focused
                and self._pointed(item)
                and (empty_for is None or empty_for < FOCUSED_READER_GRACE)
            ):
                self._set(
                    item,
                    State.WAITING,
                    f"debounce: focused reader, input must stay empty for "
                    f"{FOCUSED_READER_GRACE:.0f} s",
                )
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
                settle=settle,
                settled_for=self._settled_for(pane_id, final, now),
                terminal=item.terminal,
            )
            if self._refused(item, verdict):
                return
            if not verdict.ok:
                self._empty_for(pane_id, final.line, now)
                self._set(item, State.WAITING, f"{verdict.reason} (at final check)")
                return
            # Journalled before the write: after a crash from here on the item is replayed as
            # FAILED and a cancel is refused, so the message can never be written twice. The
            # same holds for each message typed in with it.
            riders = self._riders_of(pane_id, item)
            try:
                for rider in riders:
                    self._set(rider, State.SUBMITTING, f"writing to the pane with {item.id}")
                    self._riders.setdefault(item.id, []).append(rider.id)
                if riders:
                    self._carried[item.id] = self._merged((item, *riders))
                    self._set(
                        item,
                        State.SUBMITTING,
                        f"writing to the pane with {len(riders)} more",
                        riders=self._riders[item.id],
                    )
                else:
                    self._set(item, State.SUBMITTING, "writing to the pane")
            except OSError:
                self._unmerge(item, f"{item.id} not written: cannot journal")
                raise
            self._rejections_before[item.id] = final.rejection
            # The next message must see the box empty for a full debounce after this write.
            self.watch.setdefault(pane_id, PaneWatch()).empty_since = None
        try:
            self.herdr.submit(pane_id, self._text(item))
        except Exception as exc:  # noqa: BLE001 - every failure here must end the item
            with self.lock:
                item.attempts += 1
                code = exc.code if isinstance(exc, HerdrError) else None
                if code in SUBMIT_RETRYABLE and item.attempts < MAX_ATTEMPTS:
                    item.retry_at = self.clock() + 2.0**item.attempts
                    self._set(item, State.WAITING, f"retry {item.attempts}: {exc}")
                    self._unmerge(item, f"{item.id} not written: {exc}")
                else:
                    # Anything else may have written part of the message; resending could
                    # duplicate it, so stop and report.
                    self._finish(item, State.FAILED, f"submit failed: {exc!r}")
            return
        with self.lock:
            item.verify_until = self.clock() + VERIFY_SECONDS
            self._set(item, State.VERIFYING, "submitted, confirming")

    def _text(self, item: Item) -> str:
        """What is typed for ``item``: its text or pointer line, or the merged prompt it leads."""
        if item.id in self._carried:
            return self._carried[item.id]
        return pointer(item) if self._pointed(item) else item.text

    def _pointed(self, item: Item) -> bool:
        return (
            item.target in self.pointer_for
            and item.id not in self._unfiled
            and not _command(item.text)
        )

    def _merged(self, items: tuple[Item, ...]) -> str:
        if all(self._pointed(i) for i in items):
            return "\n".join(pointer(i) for i in items)
        return "\n\n".join(pointer(i) if self._pointed(i) else _framed(i) for i in items)

    def _riders_of(self, pane_id: str, head: Item) -> list[Item]:
        """The messages queued right behind ``head`` that go in the same prompt (lock held).

        They keep their order, so the first one that cannot go stops the rest: one in flight or
        backing off, a human message behind agent ones or the reverse, one bound to another
        receiver, one about to go back to its unrouted name, a slash command, or one past the
        size limit. A slash command at the head goes alone too."""
        queue = self.queues.get(pane_id, [])
        if self.merge_chars <= 0 or _command(head.text) or not queue or queue[0] != head.id:
            return []
        now = self.clock()
        size = len(_framed(head))
        riders = []
        for item_id in queue[1:]:
            item = self.items[item_id]
            size += 2 + len(_framed(item))
            if (
                item.state not in (State.QUEUED, State.WAITING)
                or item.retry_at > now
                or item.human != head.human
                or (item.session, item.terminal) != (head.session, head.terminal)
                or item.id in self._fallback
                or _command(item.text)
                or size > self.merge_chars
            ):
                break
            riders.append(item)
        return riders

    def _unmerge(self, head: Item, reason: str) -> None:
        """Put the messages that were to go with ``head`` back in line, unwritten (lock held)."""
        self._carried.pop(head.id, None)
        for rider_id in self._riders.pop(head.id, []):
            rider = self.items[rider_id]
            try:
                self._set(rider, State.WAITING, reason)
            except OSError:
                # Not written; a restart before the next journal line fails it as in flight.
                rider.state, rider.reason = State.WAITING, reason

    def _verify(self, item: Item, obs: Observation, now: float) -> bool:
        text = self._text(item)
        if obs.rejection and obs.rejection != self._rejections_before.get(item.id):
            self._finish(item, State.REJECTED, obs.rejection)
        elif matches(obs.line, text):
            if now < item.verify_until:
                return False
            if item.id not in self._enter_retried:
                return True
            self._finish(item, State.GHOST, "ghost: text remains after one Enter retry")
        elif holds(obs.line, text):
            if now >= item.verify_until:
                self._finish(item, State.GHOST, "ghost: edited text remains in the input box")
        elif obs.status in ("working", "blocked"):
            self._finish(item, State.SENT, f"agent {obs.status} after submit")
        elif obs.line.state is LineState.EMPTY:
            self._finish(item, State.SENT, f"input box cleared ({obs.line.evidence})")
        elif now < item.verify_until:
            return False
        else:
            self._finish(item, State.SENT, f"unconfirmed: {obs.line.evidence}")
        return False

    def _retry_enter(self, item: Item, *, manual: bool = False) -> None:
        # Re-observe outside the lock so a hold arriving during the CLI reads can be honoured.
        final = self._read(item) if manual else self._observe(item)
        with self.lock:
            if item.state in FINAL and not manual:
                return
            if final is None:
                self._finish(item, State.GHOST, "ghost: cannot confirm safe Enter retry")
                return
            if final.rejection:
                self._finish(item, State.REJECTED, final.rejection)
                if manual:
                    raise DispatchError(final.rejection)
                return
            if not matches(final.line, self._text(item)):
                if manual:
                    raise DispatchError("input no longer equals the complete queued message")
                self._verify(item, final, self.clock())
                return
            if (
                final.status not in ("idle", "done")
                or final.focused
                and not self._pointed(item)
                or self._hold_reason(item.pane_id, self.clock())
                or not (item.session or item.terminal)
                or item.session
                and final.session != item.session
                or item.terminal
                and final.terminal != item.terminal
            ):
                if manual:
                    raise DispatchError(
                        "submit refused by focus, hold, status or receiver identity"
                    )
                self._finish(item, State.GHOST, "ghost: Enter retry refused by target guards")
                return
            self._enter_retried.add(item.id)
            try:
                self._set(item, State.VERIFYING, "retrying Enter once", enter_retried=True)
            except OSError as exc:
                self._enter_retried.discard(item.id)
                self._finish(item, State.FAILED, f"cannot journal Enter intent: {exc}")
                raise DispatchError("cannot journal Enter intent; no key sent") from exc
            queue = self.queues.setdefault(item.pane_id, [])
            if manual or item.id not in queue:
                if item.id in queue:
                    queue.remove(item.id)
                queue.insert(0, item.id)
        try:
            self.herdr.enter(item.pane_id)
        except Exception as exc:  # noqa: BLE001 - a key may have landed; never retry it again
            with self.lock:
                self._finish(item, State.FAILED, f"Enter retry failed: {exc!r}")
            return
        with self.lock:
            item.verify_until = self.clock() + VERIFY_SECONDS
            self._set(item, State.VERIFYING, "Enter retried once, confirming")

    def request_submit(self, target: str) -> Future[Item]:
        pane_id = self.resolve(target)
        with self.lock:
            if pane_id not in self._submit_requests:
                self._journal({"event": "submit_request", "pane_id": pane_id})
                self._submit_requests[pane_id] = Future()
            return self._submit_requests[pane_id]

    def _submit_owned_requests(self) -> None:
        with self.lock:
            requests = list(self._submit_requests.items())
        for pane_id, future in requests:
            try:
                if not future.set_running_or_notify_cancel():
                    continue
                with self.lock:
                    items = sorted(
                        (
                            item
                            for item in self.items.values()
                            if item.pane_id == pane_id
                            and item.state not in (State.CANCELLED, State.REJECTED)
                        ),
                        key=lambda item: (item.state in IN_FLIGHT, item.updated),
                        reverse=True,
                    )
                if not items:
                    raise DispatchError("input does not equal any retained queued courier message")
                snapshot = (self.herdr.agent_get(pane_id), self.herdr.read_ansi(pane_id, lines=256))
                item = next(
                    (
                        item
                        for item in items
                        if matches(self._read(item, snapshot=snapshot).line, self._text(item))
                    ),
                    None,
                )
                if item is None:
                    raise DispatchError("input does not equal any retained queued courier message")
                self._retry_enter(item, manual=True)
                future.set_result(item)
            except Exception as exc:  # noqa: BLE001 - every request must resolve its waiting client
                future.set_exception(
                    exc
                    if isinstance(exc, DispatchError)
                    else DispatchError(f"submit request could not complete: {exc}; inspect status")
                )
            finally:
                with self.lock:
                    if self._submit_requests.get(pane_id) is future:
                        del self._submit_requests[pane_id]

    def _read(self, item: Item, *, snapshot: tuple[dict, str] | None = None) -> Observation:
        text = self._text(item)
        if snapshot is None:
            agent = self.herdr.agent_get(item.pane_id)
            lines = min(256, max(12, text.count("\n") + 7))
            screen = self.herdr.read_ansi(item.pane_id, lines=lines)
        else:
            agent, screen = snapshot
        line = classify(screen, queued=text)
        if (
            snapshot is None
            and lines < 256
            and (line.state is LineState.UNKNOWN or rejection(screen))
        ):
            screen = self.herdr.read_ansi(item.pane_id, lines=256)
            line = classify(screen, queued=text)
        return Observation(
            status=agent.get("agent_status", "unknown"),
            session=(agent.get("agent_session") or {}).get("value"),
            focused=bool(agent.get("focused")),
            line=line,
            name=agent.get("name"),
            state_seq=agent.get("state_change_seq"),
            terminal=agent.get("terminal_id"),
            rejection=rejection(screen),
        )

    def _observe(self, item: Item) -> Observation | None:
        try:
            obs = self._read(item)
        except HerdrError as exc:
            with self.lock:
                if item.state in IN_FLIGHT:
                    if self.clock() >= item.verify_until:
                        self._finish(item, State.SENT, f"unconfirmed: cannot observe: {exc}")
                    return None
                if exc.code in TARGET_GONE and item.routed_from:
                    self._route_back(item, f"{item.target} is gone")
                    return None
                if exc.code in TARGET_GONE and item.target in self.hold_missing:
                    self._hold_by_name(item)
                    return None
                if exc.code in TARGET_GONE:
                    item.attempts += 1
                    if item.attempts >= MAX_ATTEMPTS:
                        self._finish(item, State.FAILED, f"target gone: {exc}")
                        return None
                    item.retry_at = self.clock() + 2.0**item.attempts
                self._set(item, State.WAITING, f"cannot observe: {exc}")
            return None
        with self.lock:
            if obs.name in self.hold_missing and obs.session:
                self.seen[obs.name] = obs.session
            self.watch.setdefault(item.pane_id, PaneWatch()).last = (
                f"{obs.status}, {'focused, ' if obs.focused else ''}"
                f"{obs.line.state}: {obs.line.evidence}"
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

    def _settled_for(self, pane_id: str, obs: Observation, now: float) -> float:
        watch = self.watch.setdefault(pane_id, PaneWatch())
        if watch.state != obs.state or watch.state_since is None:
            watch.state, watch.state_since = obs.state, now
        return now - watch.state_since

    def _hold_reason(self, pane_id: str, now: float) -> str | None:
        hold = self.holds.get(pane_id)
        if hold is None or (hold.until is not None and hold.until <= now):
            return None
        return hold.reason

    def resolve(self, target: str) -> str:
        """Pane id for an agent name or pane id. Routes do not apply: a hold names its pane."""
        if routes.is_pane_id(target):
            return target
        try:
            return self.herdr.agent_get(target)["pane_id"]
        except HerdrError as exc:
            raise DispatchError(f"cannot resolve target {target!r}: {exc}") from exc

    def _load_routes(self) -> None:
        """The routes saved by the last run; a file that cannot be used routes nothing."""
        if self.routes_in is None:
            return
        try:
            self.routes = routes.load(self.routes_in)
        except (routes.RouteError, OSError) as exc:
            self.log(f"courier: no routes: {exc}")
            return
        if self.routes:
            self._routes_changed = True
            self.log(f"courier: routes: {', '.join(routes.describe(self.routes))}")

    def _route_changer(self, sender_pane: str) -> str:
        """Who asks for a route change, refused unless it is the controller or notice receiver in
        their own pane, a herdr pane that herdr says runs no agent (a person's shell), or a
        terminal outside herdr; an agent must not take another's messages."""
        if not sender_pane:
            return "a terminal outside herdr"
        try:
            agents = self.herdr.agent_list()
            panes = self.herdr.pane_list()
        except HerdrError as exc:
            raise DispatchError("cannot identify sender pane") from exc
        sender = next((a for a in agents if a.get("pane_id") == sender_pane), None)
        if sender is None:
            if any(p.get("pane_id") == sender_pane for p in panes):
                return f"a shell pane ({sender_pane})"
            raise DispatchError("cannot identify sender pane")
        name = sender.get("name")
        if name not in {self.controller, self.notify_to}:
            raise DispatchError(
                f"route changes need {self.controller}, {self.notify_to}, a shell pane or a "
                f"terminal outside herdr, not {name or 'an unnamed agent'} ({sender_pane})"
            )
        return f"{name} ({sender_pane})"

    def set_route(self, name: str, to: str | None, *, sender_pane: str) -> dict[str, str]:
        """Route ``name`` to ``to``, or clear its route when ``to`` is None; returns all routes.

        Saved for the next run and written to the audit log ``routes.log``. A message not yet
        delivered follows the change on the next tick."""
        who = self._route_changer(sender_pane)
        with self.lock:
            if to is None:
                if name not in self.routes:
                    return dict(self.routes)
                table = {k: v for k, v in self.routes.items() if k != name}
                what = f"{name} route cleared"
            else:
                try:
                    table = routes.with_route(self.routes, name, to)
                except routes.RouteError as exc:
                    raise DispatchError(str(exc)) from exc
                what = f"{name} → {to}"
            if self.routes_in is not None:
                try:
                    routes.save(self.routes_in, table)
                    routes.audit(self.routes_in, f"{what} by {who}")
                except OSError as exc:
                    raise DispatchError(f"route not changed: {exc}") from exc
            self.routes = table
            self._routes_changed = True
            self.log(f"courier: {what} by {who}")
            return dict(table)

    def _route(self, name: str) -> str:
        with self.lock:
            table = self.routes
        try:
            return routes.resolve(table, name)
        except routes.RouteError as exc:
            self.log(f"courier: {exc}; {name} not routed")
            return name

    def _live(self, name: str) -> dict | None:
        """The agent named ``name`` if herdr knows it with a session, as a route target must be."""
        try:
            agent = self.herdr.agent_get(name)
        except HerdrError:
            return None
        return agent if (agent.get("agent_session") or {}).get("value") else None

    def _apply_routes(self) -> None:
        """Send every undelivered message where its name is routed now, and routed messages whose
        target went away back to the name they were sent to."""
        with self.lock:
            changed, self._routes_changed = self._routes_changed, False
            fallback, self._fallback = self._fallback, set()
            if not (changed or fallback):
                return
            waiting = sorted(
                (
                    i
                    for i in self.items.values()
                    if i.state not in FINAL | IN_FLIGHT and i.state is not State.FILED
                ),
                key=lambda i: i.created,
            )
        for item in waiting:
            asked = item.routed_from or item.target
            if item.id in fallback:
                want = asked
            elif changed:
                want = self._route(asked)
            else:
                continue
            if want == item.target:
                continue
            agent = self._live(want)
            with self.lock:
                if item.state in FINAL | IN_FLIGHT:
                    continue
                if agent is not None:
                    self._move(item, want, agent, asked)
                elif want == asked and asked in self.hold_missing:
                    self._unqueue(item)
                    item.target, item.routed_from = asked, None
                    item.session, item.terminal = self.seen.get(asked), None
                    self._hold_by_name(
                        item, target=asked, routed_from=None, session=item.session, terminal=None
                    )
                elif item.id in fallback:
                    self._finish(
                        item, State.FAILED, f"{item.target} is gone and {asked} is not running"
                    )
                else:
                    self.log(
                        f"courier: {ROUTE_MISSED}{asked} → {want}, but {want} is not running; "
                        f"{item.id} stays with {item.target}"
                    )

    def _move(self, item: Item, to: str, agent: dict, asked: str) -> None:
        """Queue ``item`` for the agent ``to`` instead of where it waits now (lock held)."""
        self._unqueue(item)
        item.target, item.pane_id = to, agent["pane_id"]
        item.session = (agent.get("agent_session") or {}).get("value")
        item.terminal = agent.get("terminal_id")
        item.routed_from = asked if to != asked else None
        item.held_until, item.retry_at, item.attempts = None, 0.0, 0
        self._push(item, in_order=True)
        self._set(
            item,
            State.QUEUED,
            f"routed: {asked} → {to}" if item.routed_from else f"back to {asked}",
            target=to,
            pane_id=item.pane_id,
            session=item.session,
            terminal=item.terminal,
            routed_from=item.routed_from,
            held_until=None,
        )

    def _unqueue(self, item: Item) -> None:
        queue = self.queues.get(item.pane_id, [])
        if item.id in queue:
            queue.remove(item.id)
        if (missing := self.missing.get(item.target)) and item.id in missing.items:
            missing.items.remove(item.id)

    def _route_back(self, item: Item, why: str) -> None:
        """A routed item whose target went away goes back to its name on the next tick (lock held)."""
        self._fallback.add(item.id)
        self._set(item, State.WAITING, f"{why}; back to {item.routed_from}")

    def _stall(self, item: Item, reason: str) -> None:
        """Record a problem without leaving an in-flight state, which would allow a second write."""
        if item.state in FINAL:
            return
        self._set(item, item.state if item.state in IN_FLIGHT else State.WAITING, reason)

    def _set(self, item: Item, state: State, reason: str, **changed: Any) -> None:
        """``changed`` names the item fields the caller already changed, for the journal; they
        change only together with the state or the reason."""
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
                **changed,
            }
        )
        item.state = state
        item.reason = reason
        item.updated = time.time()
        self._log(item)
        self.lock.notify_all()

    def _finish(self, item: Item, state: State, reason: str) -> None:
        ended, rider = state, reason.startswith("with ")
        # A pointer line only wakes the reader: its message is sent when the hook shows it, so
        # however the typing ended, an unshown message stays filed rather than counted or lost.
        if state in POINTER_ENDS and self._reader_state(item) == "new":
            state, reason = State.FILED, f"pointer {state.value}: {reason}"
            self._filed_at[item.id] = self.clock()
        self._set(item, state, reason)
        state = ended
        # A rider's text is part of its head's, which is reported for both.
        if state is State.GHOST and item.id not in self._ghosted and not rider:
            self._ghosted.add(item.id)
            self._ghosts.append(item.id)
        queue = self.queues.get(item.pane_id, [])
        if item.id in queue:
            queue.remove(item.id)
        if (missing := self.missing.get(item.target)) and item.id in missing.items:
            missing.items.remove(item.id)
        # Kept after the end: a ghost can still be submitted by hand, and its riders end again.
        for rider_id in self._riders.get(item.id, ()):
            if (rider := self.items.get(rider_id)) is not None:
                self._finish(rider, state, f"with {item.id}: {reason}")
        finished = [i for i in self.items.values() if i.state in FINAL]
        for old in sorted(finished, key=lambda i: i.updated)[:-KEEP_FINISHED]:
            del self.items[old.id]
            self._enter_retried.discard(old.id)
            self._rejections_before.pop(old.id, None)
            self._carried.pop(old.id, None)
            self._ghosted.discard(old.id)
            self._riders.pop(old.id, None)

    def _record(
        self, item: Item, reason: str, *, full: bool = False, task: dict | None = None
    ) -> None:
        entry: dict[str, Any] = {
            "event": "item",
            "id": item.id,
            "state": item.state.value,
            "reason": reason,
            "attempts": item.attempts,
        }
        if full:
            entry["item"] = item.as_dict()
            # Left out when unused, so a journal without routes still replays in 0.2.0.
            if item.routed_from is None:
                del entry["item"]["routed_from"]
        if item.id in self._enter_retried:
            entry["enter_retried"] = True
        if item.id in self._riders:
            entry["riders"] = self._riders[item.id]
        if task is not None:
            entry["task"] = task
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
        siblings = list(self.journal.parent.glob(f"{stem}.{stamp}*.jsonl"))
        if siblings:
            n = max(ledger.generation_order(path)[1] for path in siblings) + 1
            dest = self.journal.with_name(f"{stem}.{stamp}-{n}.jsonl")
        self.journal.replace(dest)
        for old in sorted(
            self.journal.parent.glob(f"{stem}.[0-9]*.jsonl"), key=ledger.generation_order
        )[:-JOURNAL_KEEP]:
            old.unlink(missing_ok=True)

    def _replay(self) -> None:
        """Rebuild pending items and holds after a restart and compact the journal."""
        if not self.journal.exists() and not any(
            self.journal.parent.glob(f"{self.journal.stem}.[0-9]*.jsonl")
        ):
            return
        items: dict[str, Item] = {}
        holds: dict[str, dict[str, Any]] = {}
        riders: dict[str, list[str]] = {}
        bound: str | None = None
        for entry in ledger.history(self.journal):
            ledger.apply(self.tasks, entry)
            if entry.get("event") == "ledger-notice":
                self.ledger_notices[entry["name"]] = {
                    key: entry[key] for key in ("name", "text", "sent")
                }
            match entry.get("event"):
                case "server":
                    bound = entry.get("socket")
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
                    for key in (
                        "pane_id",
                        "terminal",
                        "held_until",
                        "target",
                        "session",
                        "routed_from",
                    ):
                        if key in entry:
                            setattr(item, key, entry[key])
            if entry.get("event") == "item" and entry.get("enter_retried"):
                self._enter_retried.add(entry["id"])
            if entry.get("event") == "item" and "riders" in entry:
                riders[entry["id"]] = entry["riders"]
        retained = sorted(
            (item for item in items.values() if item.state in FINAL),
            key=lambda item: item.updated,
        )[-KEEP_FINISHED:]
        pending = sorted(
            (i for i in items.values() if i.state not in FINAL), key=lambda i: i.created
        )
        now_wall = time.time()
        live_holds = [
            h for h in holds.values() if h.get("until_wall") is None or h["until_wall"] > now_wall
        ]
        # Pane ids are numbered per server, so another server's w1:p1 is a different pane. With
        # nothing left to deliver the state directory may serve the new server.
        if (
            self.server
            and bound
            and not same_socket(bound, self.server)
            and (
                pending
                or any(item.state is State.GHOST for item in retained)
                or live_holds
                or any(t["state"] in ledger.ACTIVE for t in self.tasks.values())
            )
        ):
            raise WrongServer(
                f"{self.journal} holds {len(pending)} queued messages and {len(live_holds)} "
                f"holds/recoverable ghosts for the herdr server at {bound}, "
                f"but herdr here reaches {self.server}. "
                f"Start courier serve where HERDR_SOCKET_PATH is {bound}, or give this server its "
                "own COURIER_STATE_DIR."
            )
        if self.journal.exists():
            self._close_generation()
        for notice in self.ledger_notices.values():
            self._journal({"event": "ledger-notice", **notice})
        for task in self.tasks.values():
            self._task_record(task)
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
        for item in sorted(items.values(), key=lambda i: i.created):
            if item.target in self.hold_missing and item.session:
                self.seen[item.target] = item.session
        kept = [item for item in retained if item.state in (State.GHOST, State.REJECTED)]
        for item in kept:
            self.items[item.id] = item
            if item.state is State.GHOST:
                self._ghosted.add(item.id)
        for item in kept:
            # A ghost of a merged prompt can still be submitted by hand; its riders must be here.
            ids = riders.get(item.id, [])
            if item.state is State.GHOST and ids and all(i in self.items for i in ids):
                self._riders[item.id] = ids
                self._carried[item.id] = self._merged((item, *(self.items[r] for r in ids)))
        for item in kept:
            self._record(item, item.reason, full=True)
        for item in pending:
            item.retry_at = 0.0
            item.updated = time.time()
            self.items[item.id] = item
            if not item.pane_id:
                # Still held for a missing agent: it stays held, and is looked up again.
                missing = self.missing.get(item.target) or self.missing.setdefault(
                    item.target, Missing(since=item.created)
                )
                missing.items.append(item.id)
                self._record(item, item.reason, full=True)
                continue
            if item.state is State.FILED or (
                item.state in IN_FLIGHT and self._reader_state(item) == "new"
            ):
                # Its text is in the mailbox, which the hook shows whatever the pane got.
                item.state = State.FILED
                self._filed_at[item.id] = self.clock()
            elif item.state in IN_FLIGHT:
                # The write may have happened before the restart and was not confirmed:
                # delivery is unknown, and writing again could duplicate it.
                item.reason = f"dispatcher restarted while {item.state}; delivery unknown"
                item.state = State.FAILED
            else:
                item.state = State.QUEUED
                item.reason = "restored after restart"
                self.queues.setdefault(item.pane_id, []).append(item.id)
            self._record(item, item.reason, full=True)
        self._enter_retried.intersection_update(self.items)
