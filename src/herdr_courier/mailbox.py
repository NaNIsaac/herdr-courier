"""A second copy of each queued message, kept per target until the target marks it done.

The journal says whether a message was typed into a pane; the mailbox says whether the agent has
seen it and finished with it, and survives compaction and restarts because it is outside the
agent's context. Each target has one append-only JSON Lines file, ``mailbox/<target>.jsonl``:
courier serve appends a ``message`` record when it queues an item (same id as the journal), and
the reader appends ``shown`` (a hook put it into the agent's context) and ``done`` (the agent
acked it). A reader's pane gets only a pointer line per message (core.queue.pointer); the hook
brings the text. Slash commands are typed whole and excluded from pending mail. See docs/mailbox.md.
"""

import json
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Per message in hook output; the full text stays in the file and in `courier inbox`.
HOOK_TEXT_CHARS = 2000
HOOK_MESSAGES = 10
# A message already shown comes again after a compaction only while it is this young; older read
# copies stay in `courier inbox` but no longer fill a new context with last week's mail.
AGAIN_SECONDS = 86400.0

_unsafe = re.compile(r"[^a-z0-9._-]")
_pointed = re.compile(r"^\[courier\] ([0-9a-f]{10}) from ", re.MULTILINE)
# How long the hook waits for a pointed message that courier serve has not filed yet.
FILED_WAIT = 2.0
# A reader's `courier inbox --wait` touches its file every second; older than this, it has ended.
WAITER_FRESH = 5.0
_lock = threading.RLock()


def mailbox_dir(root: Path) -> Path:
    return root / "mailbox"


def path_for(root: Path, target: str) -> Path:
    """``reader`` → mailbox/reader.jsonl; a pane id such as ``w3:pC`` → mailbox/w3_pc.jsonl."""
    return mailbox_dir(root) / f"{_unsafe.sub('_', target.lower())}.jsonl"


def wait_path(root: Path, target: str) -> Path:
    """The file a reader's `courier inbox --wait` keeps its token in and touches while it waits."""
    return path_for(root, target).with_suffix(".wait")


def waiter_live(root: Path, target: str, now: float) -> bool:
    """Whether a `courier inbox --wait` of ``target`` has touched its file within WAITER_FRESH."""
    try:
        return now - wait_path(root, target).stat().st_mtime < WAITER_FRESH
    except OSError:
        return False


def hook_log_path(root: Path) -> Path:
    return mailbox_dir(root) / "hook.log"


@dataclass
class Message:
    id: str
    to: str
    source: str
    created: float
    text: str
    human: bool = False
    shown: list[dict[str, Any]] = field(default_factory=list)
    done: float | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.done is not None:
            return "done"
        return "read" if self.shown else "new"


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
    with _lock, path.open("ab+") as f:
        # After a crash mid-write the file ends inside a line; start a new one rather than glue on.
        if f.seek(0, 2) and (f.seek(-1, 2), f.read(1))[1] != b"\n":
            line = b"\n" + line
        f.write(line)


def load(root: Path, target: str) -> list[Message]:
    """Every message for ``target`` in queue order, with what the reader has recorded about it."""
    try:
        lines = path_for(root, target).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    messages: dict[str, Message] = {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            # A line cut short by a crash mid-write; the next append starts a fresh line.
            continue
        match rec.get("kind"):
            case "message" if rec["id"] not in messages:
                messages[rec["id"]] = Message(
                    id=rec["id"],
                    to=rec["to"],
                    source=rec["from"],
                    created=rec["created"],
                    text=rec["text"],
                    human=rec.get("human", False),
                    attachments=rec.get("attachments", []),
                )
            case "shown" if rec["id"] in messages:
                messages[rec["id"]].shown.append({"at": rec["at"], "event": rec.get("event")})
            case "done" if rec["id"] in messages:
                messages[rec["id"]].done = rec["at"]
    return list(messages.values())


def add(root: Path, item: Any, attachments: Sequence[dict[str, Any]] = ()) -> bool:
    """Record a queued item once; a client retry that returned the same item adds nothing."""
    record = {
        "kind": "message",
        "id": item.id,
        "to": item.target,
        "from": item.source,
        "created": item.created,
        "human": item.human,
        "text": item.text,
    }
    if attachments:
        record["attachments"] = list(attachments)
    with _lock:
        if any(m.id == item.id for m in load(root, item.target)):
            return False
        _append(path_for(root, item.target), record)
    return True


def mark(root: Path, target: str, ids: list[str], kind: str, event: str | None = None) -> None:
    now = time.time()
    for message_id in ids:
        record = {"kind": kind, "id": message_id, "at": now}
        if event:
            record["event"] = event
        _append(path_for(root, target), record)


def log_hook(root: Path, line: str) -> None:
    """One line per hook call, shown or not, for the trial's timing and context-size checks."""
    path = hook_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}\n")


def pointed_ids(prompt: str) -> list[str]:
    """The ids of the pointer lines courier typed into this prompt, in order."""
    return list(dict.fromkeys(_pointed.findall(prompt)))


def load_with(root: Path, target: str, ids: list[str]) -> list[Message]:
    """``load``, waiting up to FILED_WAIT seconds for ``ids``: serve files a message just after
    queueing it, so a pointer can reach the pane a moment before its row."""
    deadline = time.monotonic() + FILED_WAIT
    while True:
        messages = load(root, target)
        known = {m.id for m in messages}
        if all(i in known for i in ids) or time.monotonic() >= deadline:
            return messages
        time.sleep(0.1)


def is_command(text: str) -> bool:
    """A slash command such as /compact, which the agent reads only as the whole prompt."""
    return text.lstrip().startswith("/")


def pending(messages: list[Message], *, again: bool, now: float | None = None) -> list[Message]:
    """New messages, and with ``again`` (after a compaction) the read ones younger than
    AGAIN_SECONDS too. Slash commands are excluded: only the whole pane prompt runs them."""
    since = (time.time() if now is None else now) - AGAIN_SECONDS
    return [
        m
        for m in messages
        if not is_command(m.text)
        and (m.state == "new" or (again and m.state == "read" and m.created >= since))
    ]


def repeat_notes(messages: list[Message], ids: list[str]) -> list[str]:
    """One line for each pointed id that is not new: it was handled once, or is not filed."""
    by_id = {m.id: m for m in messages}
    notes = []
    for i in ids:
        if i not in by_id:
            notes.append(
                f"{i} is not in the mailbox yet; read it later with courier inbox --show {i}."
            )
        elif by_id[i].state != "new":
            notes.append(
                f"{i} came again and was shown before ({by_id[i].state}); do not act on it twice."
            )
    return notes


def hook_text(
    target: str, messages: list[Message], notes: Sequence[str] = (), keep: Sequence[str] = ()
) -> str:
    """When more than HOOK_MESSAGES are open, the ones in ``keep`` (the prompt's pointer lines)
    and then the newest by time are shown, oldest first."""
    kept = [m for m in messages if m.id in keep][-HOOK_MESSAGES:]
    rest = sorted((m for m in messages if m.id not in keep), key=lambda m: m.created)
    room = HOOK_MESSAGES - len(kept)
    shown = sorted(kept + (rest[-room:] if room else []), key=lambda m: m.created)
    head = (
        f"[courier inbox] {len(messages)} message(s) for {target} not marked done. The pane gets "
        "one [courier] line per message; its text is here. "
        f"When one is handled, run: courier inbox --ack <id>"
    )
    if len(messages) > len(shown):
        head += f"\nOnly the newest {len(shown)} are below; `courier inbox` lists all."
    parts = [head] if messages else []
    if notes:
        parts.append("[courier inbox] " + " ".join(notes))
    for m in shown:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.created))
        text = m.text
        if len(text) > HOOK_TEXT_CHARS:
            text = text[:HOOK_TEXT_CHARS] + f"\n[cut; full text: courier inbox --show {m.id}]"
        parts.append(f"--- {m.id} from {m.source} at {stamp} ({m.state}) ---\n{text}")
    return "\n\n".join(parts)
