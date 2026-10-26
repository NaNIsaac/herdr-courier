"""A second copy of each queued message, kept per target until the target marks it done.

The journal says whether a message was typed into a pane; the mailbox says whether the agent has
seen it and finished with it, and survives compaction and restarts because it is outside the
agent's context. Each target has one append-only JSON Lines file, ``mailbox/<target>.jsonl``:
courier serve appends a ``message`` record when it queues an item (same id as the journal), and
the reader appends ``shown`` (a hook put it into the agent's context) and ``done`` (the agent
acked it). Typing is unchanged. See docs/mailbox.md.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Per message in hook output; the full text stays in the file and in `courier inbox`.
HOOK_TEXT_CHARS = 2000
HOOK_MESSAGES = 10

_unsafe = re.compile(r"[^a-z0-9._-]")
_lock = threading.RLock()


def mailbox_dir(root: Path) -> Path:
    return root / "mailbox"


def path_for(root: Path, target: str) -> Path:
    """``boss`` → mailbox/boss.jsonl; a pane id such as ``w3:pC`` → mailbox/w3_pc.jsonl."""
    return mailbox_dir(root) / f"{_unsafe.sub('_', target.lower())}.jsonl"


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
                )
            case "shown" if rec["id"] in messages:
                messages[rec["id"]].shown.append({"at": rec["at"], "event": rec.get("event")})
            case "done" if rec["id"] in messages:
                messages[rec["id"]].done = rec["at"]
    return list(messages.values())


def add(root: Path, item: Any) -> bool:
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


def pending(messages: list[Message], *, again: bool) -> list[Message]:
    """New messages, or with ``again`` (after a compaction) every one not yet done."""
    return [m for m in messages if m.state == "new" or (again and m.state == "read")]


def hook_text(target: str, messages: list[Message]) -> str:
    shown = messages[-HOOK_MESSAGES:]
    head = (
        f"[courier inbox] {len(messages)} message(s) for {target} not marked done. During the "
        "mailbox trial the same text is also typed into this pane as before; act on each once. "
        f"When one is handled, run: courier inbox --ack <id>"
    )
    if len(messages) > len(shown):
        head += f"\nOnly the newest {len(shown)} are below; `courier inbox` lists all."
    parts = [head]
    for m in shown:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.created))
        text = m.text
        if len(text) > HOOK_TEXT_CHARS:
            text = text[:HOOK_TEXT_CHARS] + f"\n[cut; full text: courier inbox --show {m.id}]"
        parts.append(f"--- {m.id} from {m.source} at {stamp} ({m.state}) ---\n{text}")
    return "\n\n".join(parts)
