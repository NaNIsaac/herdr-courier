"""Thin wrapper over the herdr CLI.

The socket API is not used directly: CPython on Windows has no AF_UNIX, and a CLI round trip
costs about 25 ms, which is well inside the dispatcher's one-second debounce.
"""

import json
import os
import subprocess
from typing import Any, Protocol


class HerdrError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Herdr(Protocol):
    def agent_get(self, target: str) -> dict[str, Any]: ...
    def agent_list(self) -> list[dict[str, Any]]: ...
    def read_ansi(self, pane_id: str, lines: int = 12) -> str: ...
    def submit(self, pane_id: str, text: str) -> None: ...


def _exe() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def _run(args: list[str], timeout: float) -> str:
    try:
        proc = subprocess.run(
            [_exe(), *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HerdrError("cli_timeout", f"herdr {args[0]} {args[1]} took over {timeout} s") from exc
    except OSError as exc:
        raise HerdrError("cli_missing", str(exc)) from exc
    if proc.returncode == 0:
        return proc.stdout
    raw = (proc.stderr or proc.stdout).strip()
    try:
        error = json.loads(raw)["error"]
    except (ValueError, KeyError, TypeError):
        raise HerdrError("cli_failed", raw[:500]) from None
    raise HerdrError(error.get("code", "unknown"), error.get("message", ""))


def _result(args: list[str], timeout: float = 15.0) -> dict[str, Any]:
    return json.loads(_run(args, timeout))["result"]


class CliHerdr:
    """Submits through ``herdr agent prompt``, which honours bracketed paste."""

    def agent_get(self, target: str) -> dict[str, Any]:
        return _result(["agent", "get", target])["agent"]

    def agent_list(self) -> list[dict[str, Any]]:
        return _result(["agent", "list"])["agents"]

    def read_ansi(self, pane_id: str, lines: int = 12) -> str:
        # "recent" is anchored to the bottom of the buffer; "visible" follows a scrolled
        # viewport, and "detection" drops SGR, which hides dim placeholders.
        return _run(
            [
                "pane",
                "read",
                pane_id,
                "--source",
                "recent",
                "--lines",
                str(lines),
                "--format",
                "ansi",
            ],
            15.0,
        )

    def submit(self, pane_id: str, text: str) -> None:
        _result(["agent", "prompt", pane_id, text])


class PaneHerdr(CliHerdr):
    """Submits with ``pane send-text`` plus Enter.

    For panes whose agent was registered with ``pane report-agent``: herdr refuses
    ``agent prompt`` for those, so the integration tests drive their dummy this way.
    """

    def submit(self, pane_id: str, text: str) -> None:
        _result(["pane", "send-text", pane_id, text])
        _result(["pane", "send-keys", pane_id, "enter"])
