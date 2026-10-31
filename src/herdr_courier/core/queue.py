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
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .. import ledger, mailbox, probes
from .gate import Observation, check
from .inputline import InputLine, LineState, classify, holds, matches
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
    terminal: str | None = None
    # Wall time after which a held item fails. An item held for a missing agent has no pane
    # (pane_id "") until the agent is back.
    held_until: float | None = None


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
        controller: str = "boss",
        secretary: str = "secretary",
    ) -> None:
        """``settle_for`` maps an agent name to its between-turns settle time, or None (gate).

        ``server`` is the herdr socket every call of ``herdr`` reaches; the journal records it
        so that queued work is never replayed against another server's pane ids.

        A message for an agent in ``hold_missing`` that herdr does not know, or that lost its
        session, is held for ``hold_ttl`` seconds instead of failing, and the agent named for it
        in ``summary_to`` is told how many are held. ``wall`` is the clock of those deadlines,
        which survive a restart."""
        self.herdr = herdr
        self.journal = journal
        self.clock = clock
        self.wall = wall
        self.controller = controller
        self.secretary = secretary
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
        self.queues: dict[str, list[str]] = {}
        self.holds: dict[str, Hold] = {}
        self.watch: dict[str, PaneWatch] = {}
        self.missing: dict[str, Missing] = {}
        # The session each agent in hold_missing last had, for messages queued while it is gone.
        self.seen: dict[str, str | None] = {}
        self.tasks: dict[str, dict] = {}
        self.ledger_notices: dict[str, dict] = {}
        self._last_compact_size = 0
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
    ) -> Item:
        """Queue ``text`` for ``target``. ``hold=False`` refuses instead of holding it for a
        missing agent, so courier's own summaries never wait on one another."""
        if not text.strip():
            raise DispatchError("empty message")
        if client_id and (known := self._by_client_id(client_id)):
            return known
        try:
            agent = self.herdr.agent_get(target)
        except HerdrError as exc:
            if hold and target in self.hold_missing and exc.code in TARGET_GONE:
                return self._enqueue_held(target, text, source, human, client_id, task=task)
            raise DispatchError(f"cannot resolve target {target!r}: {exc}") from exc
        session = (agent.get("agent_session") or {}).get("value")
        if target in self.hold_missing:
            with self.lock:
                self.seen[target] = session
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
            terminal=agent.get("terminal_id"),
        )
        with self.lock:
            # A retried request may have raced the first one while the target was resolved.
            if client_id and (known := self._by_client_id(client_id)):
                return known
            if task is not None:
                task["dispatch_id"] = item.id
            self._record(item, "queued", full=True, task=task)
            self.items[item.id] = item
            if task is not None:
                self.tasks[task["key"]] = task
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
                target, text, source=source, human=human, client_id=client_id, task=task
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
        if sender.get("pane_id") != sender_pane or sender.get("name") != self.controller:
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
            if not {"lane_state_task", "descendants_working"} <= agent.keys():
                reasons.append("execution proof unavailable")
            if agent.get("agent_status") == "working" or agent.get("descendants_working"):
                reasons.append(f"{task['to']} working")
            elif agent.get("agent_status") not in {"idle", "missing"}:
                reasons.append(f"{task['to']} execution state uncertain")
            if agent.get("lane_state_task") == key:
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
                task["override"] = reasons or ["boss --force"]
                task["override_reason"] = reason
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

    def _secretary_notice(self, text: str, client_id: str) -> None:
        item = Item(
            id=hashlib.sha256(client_id.encode()).hexdigest()[:10],
            target=self.secretary,
            pane_id="",
            session=None,
            text=text,
            source="courier",
            human=False,
            created=ledger.time.time(),
        )
        if mailbox.add(self.journal.parent, item):
            try:
                self.enqueue(self.secretary, text, source="courier", client_id=client_id)
            except DispatchError as exc:
                self.log(f"secretary notice kept in mailbox: {exc}")

    def _flush_task_notices(self) -> None:
        for notice in self.ledger_notices.values():
            if not notice["sent"]:
                try:
                    self._secretary_notice(notice["text"], "ledger:" + notice["name"])
                except OSError as exc:
                    self.log(f"secretary notification pending: {exc}")
                    continue
                notice["sent"] = True
                self._journal({"event": "ledger-notice", **notice})
        for task in self.tasks.values():
            if text := task.get("secretary_notice"):
                client_id = f"force:{task['key']}:{task['attempts']}"
                try:
                    self._secretary_notice(text, client_id)
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
            names = ledger.role_names()
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
    ) -> Item:
        with self.lock:
            if client_id and (known := self._by_client_id(client_id)):
                return known
            missing = self._missing(target)
            item = Item(
                id=uuid.uuid4().hex[:10],
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

    def _hold_by_name(self, item: Item) -> None:
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
            item, State.WAITING, missing.reason(item.target), pane_id="", held_until=item.held_until
        )

    def _refused(self, item: Item, verdict) -> bool:
        """Fail or hold an item the gate refused for its identity; True if it did (lock held).

        A missing session may be the same agent restarting (a resume), so the item waits for it
        until ``hold_ttl``; another session fails it, since it is another agent. For an agent in
        ``hold_missing`` both wait, by name, and only a return with the same session sends it.
        """
        if not (verdict.fatal or verdict.lost):
            return False
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
            held = {
                name: {
                    "since": m.since,
                    "back": m.back,
                    "pending": [asdict(self.items[i]) for i in m.items],
                }
                for name, m in self.missing.items()
                if pane is None and m.items
            }
            return {"panes": panes, "held": held, "recent": [asdict(i) for i in finished]}

    def tick(self) -> None:
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
        if self.missing:
            self._look_for_missing()
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
            verdict = check(
                obs,
                session=item.session,
                hold=self._hold_reason(pane_id, now),
                empty_for=self._empty_for(pane_id, obs.line, now),
                settle=settle,
                settled_for=self._settled_for(pane_id, obs, now),
                terminal=item.terminal,
            )
            if self._refused(item, verdict):
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

    def _verify(self, item: Item, obs: Observation, now: float) -> bool:
        if "\n" in item.text and matches(obs.line, item.text):
            if now < item.verify_until:
                return False
            if item.id not in self._enter_retried:
                return True
            self._finish(item, State.FAILED, "stuck_in_input: text remains after one Enter retry")
        elif obs.status in ("working", "blocked"):
            self._finish(item, State.SENT, f"agent {obs.status} after submit")
        elif obs.line.state is LineState.EMPTY:
            self._finish(item, State.SENT, f"input box cleared ({obs.line.evidence})")
        elif now < item.verify_until:
            return False
        elif holds(obs.line, item.text):
            # Enter did not take. Leave the text for a human to see; never resend.
            self._finish(item, State.FAILED, "stuck_in_input: text is still in the input box")
        else:
            self._finish(item, State.SENT, f"unconfirmed: {obs.line.evidence}")
        return False

    def _retry_enter(self, item: Item) -> None:
        # Re-observe outside the lock so a hold arriving during the CLI reads can be honoured.
        final = self._observe(item)
        with self.lock:
            if item.state in FINAL:
                return
            if final is None:
                self._finish(item, State.FAILED, "stuck_in_input: cannot confirm safe Enter retry")
                return
            if not matches(final.line, item.text):
                self._verify(item, final, self.clock())
                return
            if (
                final.status not in ("idle", "done")
                or final.focused
                or self._hold_reason(item.pane_id, self.clock())
                or not (item.session or item.terminal)
                or item.session
                and final.session != item.session
                or item.terminal
                and final.terminal != item.terminal
            ):
                self._finish(
                    item, State.FAILED, "stuck_in_input: Enter retry refused by target guards"
                )
                return
            self._enter_retried.add(item.id)
            self._set(item, State.VERIFYING, "retrying Enter once", enter_retried=True)
        try:
            self.herdr.enter(item.pane_id)
        except Exception as exc:  # noqa: BLE001 - a key may have landed; never retry it again
            with self.lock:
                self._finish(item, State.FAILED, f"Enter retry failed: {exc!r}")
            return
        with self.lock:
            item.verify_until = self.clock() + VERIFY_SECONDS
            self._set(item, State.VERIFYING, "Enter retried once, confirming")

    def _observe(self, item: Item) -> Observation | None:
        try:
            agent = self.herdr.agent_get(item.pane_id)
            lines = min(256, max(12, item.text.count("\n") + 7))
            line = classify(self.herdr.read_ansi(item.pane_id, lines=lines))
        except HerdrError as exc:
            with self.lock:
                if item.state in IN_FLIGHT:
                    if self.clock() >= item.verify_until:
                        self._finish(item, State.SENT, f"unconfirmed: cannot observe: {exc}")
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
        obs = Observation(
            status=agent.get("agent_status", "unknown"),
            session=(agent.get("agent_session") or {}).get("value"),
            focused=bool(agent.get("focused")),
            line=line,
            name=agent.get("name"),
            state_seq=agent.get("state_change_seq"),
            terminal=agent.get("terminal_id"),
        )
        with self.lock:
            if obs.name in self.hold_missing and obs.session:
                self.seen[obs.name] = obs.session
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
        queue = self.queues.get(item.pane_id, [])
        if item.id in queue:
            queue.remove(item.id)
        if (missing := self.missing.get(item.target)) and item.id in missing.items:
            missing.items.remove(item.id)
        self._set(item, state, reason)
        finished = [i for i in self.items.values() if i.state in FINAL]
        for old in sorted(finished, key=lambda i: i.updated)[:-KEEP_FINISHED]:
            del self.items[old.id]
            self._enter_retried.discard(old.id)

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
            entry["item"] = asdict(item)
        if item.id in self._enter_retried:
            entry["enter_retried"] = True
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
                    for key in ("pane_id", "terminal", "held_until"):
                        if key in entry:
                            setattr(item, key, entry[key])
            if entry.get("event") == "item" and entry.get("enter_retried"):
                self._enter_retried.add(entry["id"])
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
                or live_holds
                or any(t["state"] in ledger.ACTIVE for t in self.tasks.values())
            )
        ):
            raise WrongServer(
                f"{self.journal} holds {len(pending)} queued messages and {len(live_holds)} "
                f"holds for the herdr server at {bound}, but herdr here reaches {self.server}. "
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
        self._enter_retried.intersection_update(self.items)
