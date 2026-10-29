"""Task records carried by the dispatcher's journal and single-writer lock."""

import json
import math
import os
import re
import shlex
import time
from pathlib import Path

ACTIVE = frozenset({"OPEN", "VERIFYING", "BLOCKED"})
KEY = re.compile(r"(?:BL-\d+|[\w.-]+#\d+|Q\d+)", re.ASCII)
FIELDS = ("due", "repo", "branch", "cwd", "needs_approval", "approval_ref")
DETAIL_LIMIT = 2000
TOMBSTONE_LIMIT = 50_000
JOURNAL_BYTES = 4 * 1024 * 1024


def new(key: str, source: str, target: str, evidence: list[str], **fields) -> dict:
    if not isinstance(key, str) or len(key) > 128 or not KEY.fullmatch(key):
        raise ValueError("key must be a BACKLOG id, repo#n or Q number")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(e, str) or not e.strip() for e in evidence)
    ):
        raise ValueError("at least one evidence pointer is required")
    if len(evidence) > 16 or any(len(e) > 2048 for e in evidence):
        raise ValueError("evidence exceeds 16 pointers or 2048 characters per pointer")
    fields = {name: fields[name] for name in FIELDS if name in fields}
    for name in ("repo", "branch", "cwd", "approval_ref"):
        value = fields.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > 2048):
            raise ValueError(f"invalid {name}")
    if not isinstance(fields.get("needs_approval", False), bool):
        raise TypeError("needs_approval must be true or false")
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


def evidence_exists(task: dict) -> bool:
    cwd = Path(task.get("cwd") or ".")
    for pointer in task["evidence"]:
        parts = shlex.split(pointer)
        if parts and parts[0] in {"file", "log"} and len(parts) >= 2:
            if (cwd / parts[1]).exists():
                return True
        elif parts and parts[0] not in {"merge", "send", "deploy", "delete", "post"}:
            raise ValueError(f"cannot check evidence locally: {pointer}")
    branch = task.get("branch")
    if not branch:
        return False
    if any(p in {"", ".", ".."} for p in branch.split("/")) or "\\" in branch:
        raise ValueError("invalid branch")
    gitdir = cwd / ".git"
    if gitdir.is_file():
        prefix, _, path = gitdir.read_text(encoding="utf-8").partition(": ")
        if prefix != "gitdir":
            raise ValueError("invalid gitdir")
        gitdir = cwd / path.strip()
    common = gitdir / "commondir"
    if common.is_file():
        gitdir = gitdir / common.read_text(encoding="utf-8").strip()
    if (gitdir / "refs" / "heads" / branch).exists():
        return True
    packed = gitdir / "packed-refs"
    if packed.exists():
        return any(
            line.endswith(f" refs/heads/{branch}")
            for line in packed.read_text(encoding="utf-8").splitlines()
        )
    return False


def apply(tasks: dict, entry: dict) -> None:
    task = entry.get("task") if entry.get("event") == "task" else None
    if entry.get("event") == "item":
        task = entry.get("task")
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


def generation_order(path: Path) -> tuple[str, int]:
    match = re.search(r"\.(\d{8}T\d{6}Z)(?:-(\d+))?\.jsonl$", path.name)
    if match is None:
        raise ValueError(f"invalid journal generation name: {path.name}")
    return match[1], int(match[2] or 0)


def history(path: Path):
    generations = sorted(path.parent.glob(f"{path.stem}.[0-9]*.jsonl"), key=generation_order)
    readable = False
    for candidate in [*generations, path]:
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        readable = True
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry
    if not readable:
        raise FileNotFoundError("no readable journal generation")


def read_history(path: Path) -> dict:
    tasks = {}
    for entry in history(path):
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
