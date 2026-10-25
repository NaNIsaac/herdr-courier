"""courier doctor: what is wrong with herdr, courier serve and the tools around them, read only.

Every check only reads: herdr's CLI, courier serve's endpoint file, status route and journal,
config.toml, the endpoint files of other programs that write prompts into panes
(``doctor.other_writers``), and Ovis's unauthenticated health route (``ovis.url``). No
lock is taken, so a doctor run cannot stop a courier serve that is starting at the same moment.
"""

import json
import os
import subprocess
import time
import urllib.error
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from . import display, ovis
from .client import LostContact, Refused, Unavailable, request
from .config import Config, ConfigError, Ovis, config_path, load
from .core.transport import CliHerdr, HerdrError
from .state import endpoint_path, journal_path, state_dir

STALE_TICK = 10.0
PENDING_TOO_LONG = 600.0
RECENT_FAILURE = 3600.0
JOURNAL_TOO_BIG = 10 * 1024 * 1024
START_DAEMON = "start courier serve in a herdr shell pane (README, Run)"


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class Probes(Protocol):
    def herdr_version(self) -> str: ...
    def pane_list(self) -> list[dict[str, Any]]: ...
    def agent_list(self) -> list[dict[str, Any]]: ...
    def daemon_status(self, root: Path) -> dict[str, Any]: ...
    def pid_alive(self, pid: int) -> bool: ...
    def ovis_health(self, url: str) -> dict[str, Any]: ...


class LiveProbes:
    def __init__(self) -> None:
        self._herdr = CliHerdr()

    def herdr_version(self) -> str:
        exe = os.environ.get("HERDR_BIN_PATH") or "herdr"
        try:
            proc = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HerdrError("cli_missing", str(exc)) from exc
        if proc.returncode != 0:
            raise HerdrError("cli_failed", (proc.stderr or proc.stdout).strip()[:200])
        return proc.stdout.strip().removeprefix("herdr ").strip()

    def pane_list(self) -> list[dict[str, Any]]:
        return self._herdr.pane_list()

    def agent_list(self) -> list[dict[str, Any]]:
        return self._herdr.agent_list()

    def daemon_status(self, root: Path) -> dict[str, Any]:
        return request("GET", "status", timeout=5.0, root=root)

    def pid_alive(self, pid: int) -> bool:
        return pid_alive(pid)

    def ovis_health(self, url: str) -> dict[str, Any]:
        return ovis.health(url)


def pid_alive(pid: int) -> bool:
    """Whether a process with this pid exists; a reused pid reads as alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) on Windows terminates the process, so ask the kernel instead.
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _endpoint(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_herdr(probes: Probes) -> tuple[Check, str | None]:
    try:
        version = probes.herdr_version()
    except HerdrError as exc:
        return Check(
            "herdr", Level.FAIL, f"no herdr CLI ({exc})", "install herdr or set HERDR_BIN_PATH"
        ), None
    try:
        panes, agents = probes.pane_list(), probes.agent_list()
    except HerdrError as exc:
        return (
            Check(
                "herdr",
                Level.FAIL,
                f"{version}; the server does not answer ({exc})",
                "start herdr; nothing can be sent until it answers",
            ),
            version,
        )
    blocked = sum(a.get("agent_status") == "blocked" for a in agents)
    detail = f"{version}; {len(panes)} panes, {len(agents)} agents"
    if blocked:
        return Check("herdr", Level.WARN, f"{detail}, {blocked} blocked on a dialog"), version
    return Check("herdr", Level.OK, detail), version


def check_daemon(probes: Probes, root: Path, now: float) -> list[Check]:
    endpoint = _endpoint(endpoint_path(root))
    if endpoint is None:
        return [
            Check(
                "dispatcher", Level.FAIL, f"not running (no endpoint file in {root})", START_DAEMON
            )
        ]
    pid = int(endpoint.get("pid") or 0)
    alive = probes.pid_alive(pid)
    try:
        reply = probes.daemon_status(root)
    except (Unavailable, LostContact, Refused) as exc:
        if not alive:
            return [
                Check(
                    "dispatcher",
                    Level.FAIL,
                    f"not running (pid {pid} has exited; stale endpoint file)",
                    START_DAEMON,
                )
            ]
        return [
            Check(
                "dispatcher",
                Level.FAIL,
                f"pid {pid} is alive but does not answer: {exc}",
                "read the courier serve pane; if it is stuck, stop it with Ctrl-C and start it again",
            )
        ]
    checks = [
        Check("dispatcher", Level.OK, f"pid {pid}, port {endpoint.get('port')}, api {reply['api']}")
    ]
    worker = reply.get("worker") or {}
    tick = worker.get("seconds_since_tick")
    if not worker.get("alive") or tick is None or tick > STALE_TICK:
        checks.append(
            Check(
                "worker",
                Level.FAIL,
                f"not dispatching (alive={worker.get('alive')}, last pass {tick} s ago)",
                "restart courier serve; queued items are kept in its journal",
            )
        )
    elif worker.get("errors"):
        checks.append(
            Check(
                "worker",
                Level.WARN,
                f"{worker['errors']} failed passes, last: {worker.get('last_error')}",
            )
        )
    else:
        checks.append(Check("worker", Level.OK, f"last pass {tick} s ago, no errors"))
    checks.append(check_queue(reply["status"], now))
    return checks


def check_queue(status: dict[str, Any], now: float) -> Check:
    pending = [i for p in status["panes"].values() for i in p["pending"]]
    holds = {pane: p["hold"] for pane, p in status["panes"].items() if p["hold"]}
    slow = [i for i in pending if now - i["created"] > PENDING_TOO_LONG]
    forever = sorted(pane for pane, h in holds.items() if h.get("seconds_left") is None)
    failed = [
        i
        for i in status["recent"]
        if i["state"] == "failed" and now - i["updated"] < RECENT_FAILURE
    ]
    detail = f"{len(pending)} queued, {len(holds)} held"
    problems = []
    if slow:
        oldest = max(slow, key=lambda i: now - i["created"])
        problems.append(
            f"{len(slow)} waiting over {int(PENDING_TOO_LONG // 60)} min "
            f"(oldest {oldest['id']}: {oldest['reason']})"
        )
    if forever:
        problems.append(f"held with no expiry: {', '.join(forever)}")
    if failed:
        problems.append(f"{len(failed)} failed in the last hour (last: {failed[-1]['reason']})")
    if problems:
        return Check(
            "queue", Level.WARN, "; ".join([detail, *problems]), "courier status shows each item"
        )
    return Check("queue", Level.OK, detail)


def check_journal(root: Path) -> Check:
    path = journal_path(root)
    if not path.exists():
        return Check("journal", Level.OK, "empty (nothing queued since courier serve started)")
    size = path.stat().st_size
    with path.open(encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    broken = 0
    for raw in lines:
        try:
            json.loads(raw)
        except ValueError:
            broken += 1
    detail = f"{len(lines)} entries, {size / 1024:.0f} KiB"
    if broken:
        return Check(
            "journal",
            Level.WARN,
            f"{detail}, {broken} unreadable (skipped on replay)",
            f"inspect {path}",
        )
    if size > JOURNAL_TOO_BIG:
        return Check("journal", Level.WARN, f"{detail}; compacted only when courier serve restarts")
    return Check("journal", Level.OK, detail)


def check_config(root: Path, config: Config | None) -> tuple[Check, Config]:
    if config is not None:
        source = str(config.path) if config.path else "built-in defaults"
        return Check("config", Level.OK, source), config
    path = config_path(root)
    try:
        config = load(path)
    except ConfigError as exc:
        return Check(
            "config", Level.FAIL, str(exc), "fix or remove the file; the checks below use defaults"
        ), Config()
    if config.path is None:
        return Check("config", Level.OK, "built-in defaults (no config.toml)"), config
    return Check("config", Level.OK, "config.toml in the state directory"), config


def check_other_writers(probes: Probes, paths: tuple[str, ...]) -> Check:
    """Fail while another program that writes prompts into panes is running."""
    if not paths:
        return Check("writers", Level.OK, "no other writers configured")
    live, stale = [], 0
    for raw in paths:
        path = Path(raw).expanduser()
        endpoint = _endpoint(path)
        if endpoint is None:
            continue
        pid = int(endpoint.get("pid") or 0)
        if probes.pid_alive(pid):
            live.append(f"pid {pid} ({path})")
        else:
            stale += 1
    if live:
        return Check(
            "writers",
            Level.FAIL,
            f"another dispatcher is running: {', '.join(live)}; two pane writers",
            "stop it, or make it send through courier serve",
        )
    detail = "no other dispatcher running"
    return Check("writers", Level.OK, f"{detail} ({stale} stale endpoint)" if stale else detail)


def check_ovis(probes: Probes, settings: Ovis, herdr_version: str | None) -> Check:
    url = settings.url
    try:
        health = probes.ovis_health(url)
    except (OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return Check(
            "ovis", Level.WARN, f"no answer from {url} ({reason}); courier serve does not need it"
        )
    if not health.get("ok"):
        return Check("ovis", Level.WARN, f"health is not ok: {health}")
    seen = (health.get("herdr") or {}).get("version")
    revision = str((health.get("web_ui") or {}).get("revision") or "?")[:7]
    detail = f"ok, revision {revision}, sees herdr {seen}"
    level = Level.OK
    if herdr_version and seen and seen != herdr_version:
        level, detail = Level.WARN, f"{detail}, CLI is {herdr_version}"
    if settings.panes:
        detail += f"; between turns only: {', '.join(settings.panes)}"
    return Check("ovis", level, detail)


def diagnose(
    probes: Probes,
    *,
    root: Path | None = None,
    config: Config | None = None,
    now: float | None = None,
) -> list[Check]:
    """Every check in order; ``config`` replaces reading config.toml from ``root``."""
    root = root or state_dir()
    now = time.time() if now is None else now
    herdr, version = check_herdr(probes)
    settings, config = check_config(root, config)
    checks = [
        herdr,
        *check_daemon(probes, root, now),
        check_journal(root),
        settings,
        check_other_writers(probes, config.doctor.other_writers),
    ]
    if config.ovis.url:
        checks.append(check_ovis(probes, config.ovis, version))
    return checks


def render(checks: list[Check], width: int) -> str:
    fails = sum(c.level is Level.FAIL for c in checks)
    warns = sum(c.level is Level.WARN for c in checks)
    summary = "all checks pass" if not fails + warns else f"{fails} fail, {warns} warn"
    out = [f"courier doctor: {summary}"]
    name_w = max(len(c.name) for c in checks)
    indent = " " * (4 + 1 + name_w + 1)
    for c in checks:
        out += display.wrap(c.detail, width, f"{c.level:4} {c.name:{name_w}} ", indent)
        if c.fix:
            out += display.wrap(c.fix, width, f"{indent}fix: ", indent + "     ")
    return "\n".join(out)
