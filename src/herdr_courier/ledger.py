"""Task records carried by the dispatcher's journal and single-writer lock."""

import json
import math
import os
import re
import time
from pathlib import Path

ACTIVE = frozenset({"OPEN", "VERIFYING", "BLOCKED"})
KEY = re.compile(r"(?:BL-\d+|[\w.-]+#\d+|Q\d+)", re.ASCII)


def new(key: str, source: str, target: str, evidence: list[str], **fields) -> dict:
    if not KEY.fullmatch(key):
        raise ValueError("key must be a BACKLOG id, repo#n or Q number")
    if not evidence or any(not isinstance(e, str) or not e.strip() for e in evidence):
        raise ValueError("at least one evidence pointer is required")
    now = time.time()
    due = fields.pop("due", None)
    if due is not None and (
        isinstance(due, bool) or not isinstance(due, int | float) or not math.isfinite(due)
    ):
        raise ValueError("due must be a finite timestamp")
    return {
        "key": key,
        "from": source,
        "to": target,
        "sent": now,
        "due": due,
        "state": "OPEN",
        "attempts": 0,
        "evidence": evidence,
        "needs_approval": False,
        "approval_ref": None,
        "repo": None,
        "branch": None,
        "cwd": None,
        "closed_by": None,
        "note": "",
        "flags": [],
        **fields,
    }


def role_names() -> set[str] | None:
    path = os.environ.get("COURIER_ROLES_FILE")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return set(data["lanes"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def apply(tasks: dict, entry: dict) -> None:
    task = entry.get("task") if entry.get("event") == "task" else None
    if entry.get("event") == "item":
        task = entry.get("item", {}).get("task")
    if isinstance(task, dict) and "key" in task:
        tasks[task["key"]] = task


def read(path: Path) -> dict:
    tasks = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            apply(tasks, entry)
    return tasks


def pending(root: Path, task: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(task, ensure_ascii=False) + "\n").encode()
    with (root / "pending-dispatch.jsonl").open("ab") as stream:
        stream.write(data)


def text(tasks: list[dict], banner: str = "") -> str:
    now = time.time()
    lines = [banner] if banner else []
    for task in tasks:
        lines.append(
            f"{task['key']} {task['state']} owner={task['to']} "
            f"age={max(0, int(now - task['sent']))}s due={task.get('due')}"
        )
    return "\n".join(lines or ["nothing open"])
