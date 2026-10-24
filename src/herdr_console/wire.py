"""Where hcd keeps its state, and the one-request-per-connection JSON protocol to reach it."""

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any


class Unavailable(Exception):
    """hcd is not running or not reachable; nothing was sent."""


class LostContact(Exception):
    """The connection to hcd broke after the request was written."""


def state_dir() -> Path:
    if override := os.environ.get("HC_STATE_DIR"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "herdr-console"


def endpoint_file() -> Path:
    return state_dir() / "hcd.json"


def request(op: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    path = endpoint_file()
    try:
        endpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Unavailable(f"no endpoint file {path} ({exc.__class__.__name__})") from exc
    body = json.dumps({**(params or {}), "op": op, "token": endpoint["token"]}, ensure_ascii=False)
    try:
        sock = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=timeout)
    except OSError as exc:
        raise Unavailable(f"cannot reach hcd on port {endpoint['port']}: {exc}") from exc
    # Once connected the request may already have been acted on, so later failures must not
    # be reported as "nothing was sent".
    with sock:
        try:
            sock.sendall(body.encode("utf-8") + b"\n")
            reply = sock.makefile("r", encoding="utf-8").readline()
        except OSError as exc:
            raise LostContact(f"hcd stopped responding: {exc}") from exc
    if not reply:
        raise LostContact("hcd closed the connection without a reply")
    return json.loads(reply)
