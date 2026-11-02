"""Files sent with a message, copied into this PC's inbox before the message is queued.

``courier send`` runs on the PC whose courier serve types the message (over ssh when the sender
is elsewhere), so the copy lands where the receiving agent can open it. A file on another PC is
named ``<machine>:<path>`` and fetched with scp. The message names only the local copy; where it
came from, its size and hash follow as information.
"""

import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .state import state_dir

MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 10
MAX_TOTAL = 100 * 1024 * 1024
SCP_TIMEOUT = 300.0
# One letter before the colon is a Windows drive, so a machine name needs at least two.
_REMOTE = re.compile(r"(?P<machine>[A-Za-z0-9][A-Za-z0-9._-]+):(?P<path>.+)")


class AttachError(Exception):
    """A file could not be copied; nothing was sent and nothing is left in the inbox."""


@dataclass(frozen=True)
class Attachment:
    path: str
    from_machine: str
    from_path: str
    size: int
    sha256: str


def inbox_dir(setting: str) -> Path:
    """The configured inbox with ``~`` and environment variables expanded, else the state dir's."""
    if not setting:
        return state_dir() / "inbox"
    path = Path(os.path.expandvars(os.path.expanduser(setting)))
    if not path.is_absolute():
        raise AttachError(f"files.inbox must be an absolute path, not {setting!r}")
    return path


def split(spec: str) -> tuple[str | None, str]:
    """``pc-b:C:/a.png`` is (``pc-b``, ``C:/a.png``); ``C:/a.png`` is local."""
    if m := _REMOTE.fullmatch(spec):
        return m["machine"], m["path"]
    return None, spec


def scp(machine: str, path: str, folder: Path, name: str) -> None:
    # Run in the folder with a ./ name: a local Windows path such as X:\... reads to scp as a host.
    try:
        done = subprocess.run(
            ["scp", "-q", "-B", f"{machine}:{path.replace('\\', '/')}", f"./{name}"],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=SCP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AttachError(f"scp from {machine} took over {SCP_TIMEOUT:g} s") from exc
    if done.returncode:
        raise AttachError(f"scp from {machine} failed: {done.stderr.strip() or done.returncode}")


def stage(
    specs: list[str], inbox: Path, msg_id: str, *, now: float | None = None, fetch=scp
) -> tuple[Path, list[Attachment]]:
    """Copy every file into ``<inbox>/YYYYMMDD-HHMMSS-<msg_id>/`` under its own name.

    All or nothing: on the first failure the folder is removed and AttachError says why.
    """
    if len(specs) > MAX_FILES:
        raise AttachError(f"{len(specs)} files; at most {MAX_FILES} go with one message")
    sources = [split(s) for s in specs]
    names = [re.split(r"[\\/]", path)[-1] for _, path in sources]
    for spec, name in zip(specs, names, strict=True):
        if not name:
            raise AttachError(f"{spec}: not a file name")
    if dup := next((n for n in names if names.count(n) > 1), None):
        raise AttachError(f"two files named {dup}; one folder holds each name once")
    total = 0
    for (machine, path), spec in zip(sources, specs, strict=True):
        if machine is None:
            if not Path(path).is_file():
                raise AttachError(f"{spec}: no such file")
            total += _check_size(spec, Path(path).stat().st_size, total)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    folder = inbox / f"{stamp}-{msg_id}"
    try:
        folder.mkdir(parents=True)
    except OSError as exc:
        raise AttachError(f"cannot create {folder}: {exc}") from exc
    here = socket.gethostname()
    done: list[Attachment] = []
    try:
        total = 0
        for (machine, path), name, spec in zip(sources, names, specs, strict=True):
            dest = folder / name
            if machine is None:
                shutil.copyfile(path, dest)
                origin = str(Path(path).resolve())
            else:
                fetch(machine, path, folder, name)
                origin = path
            size = dest.stat().st_size
            total += _check_size(spec, size, total)
            with dest.open("rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            done.append(Attachment(str(dest), machine or here, origin, size, digest))
    except (OSError, AttachError) as exc:
        shutil.rmtree(folder, ignore_errors=True)
        raise AttachError(exc if isinstance(exc, AttachError) else f"{spec}: {exc}") from exc
    return folder, done


def _check_size(spec: str, size: int, before: int) -> int:
    if size > MAX_FILE:
        raise AttachError(f"{spec}: {human(size)}, over {human(MAX_FILE)} per file")
    if before + size > MAX_TOTAL:
        raise AttachError(f"files total over {human(MAX_TOTAL)} per message")
    return size


def human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB"


def line(a: Attachment) -> str:
    return (
        f"file: {a.path} (from {a.from_machine} {a.from_path}, {human(a.size)}, "
        f"sha256 {a.sha256[:12]})"
    )
