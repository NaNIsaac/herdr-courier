"""courier serve: the one process that writes prompts into herdr panes.

Run it in a herdr shell pane, not in an agent's background shell, so it outlives agent sessions.
It listens on 127.0.0.1 only, takes an OS lock on its state directory so a second instance cannot
replay the same journal, and writes an endpoint file (port, per-start token, pid) that courier reads.
One worker thread calls ``Dispatcher.tick``; it is the only writer to any pane.
"""

import argparse
import hmac
import io
import json
import math
import os
import secrets
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import mailbox, ovis
from .config import ConfigError, config_path, load
from .core.queue import Dispatcher, DispatchError, LedgerCapacity, WrongServer
from .core.transport import CliHerdr, HerdrError, PaneHerdr, same_socket
from .state import adopt_old_state, endpoint_path, journal_path, lock_path, state_dir

TICK = 0.5
MAX_BODY = 64 * 1024
# Bumped when a route or field changes meaning; clients refuse a version they do not know.
API_VERSION = 1
MAX_HOLD_SECONDS = 7 * 24 * 3600
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(Exception):
    """Another process already dispatches from this state directory."""


class InstanceLock:
    """An OS file lock on the state directory, held for the life of the process.

    Two dispatchers on one journal would replay each other's items and both write to panes.
    The OS drops the lock when the process dies, so a crash leaves nothing to clean up.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a+")
        try:
            self._file.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            raise AlreadyRunning(
                f"another courier serve already dispatches from {path.parent}; stop it first"
            ) from exc

    def release(self) -> None:
        if self._file.closed:
            return
        try:
            self._file.seek(0)
            if os.name == "nt":
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._file, fcntl.LOCK_UN)
        finally:
            self._file.close()


class DispatchService:
    """Owns the dispatcher, the worker thread that is its only pane writer, and courier's token."""

    def __init__(
        self,
        dispatcher: Dispatcher | None,
        root: Path,
        tick: float = TICK,
        lock: InstanceLock | None = None,
        mailbox: bool = False,
    ) -> None:
        self.dispatcher = dispatcher
        self.root = root
        self.tick = tick
        self.lock = lock
        self.mailbox = mailbox
        self.token = secrets.token_urlsafe(24)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wrote_endpoint = False
        self.last_tick: float | None = None
        self.last_error: str | None = None
        self.errors = 0

    def start(self, port: int) -> None:
        if self.dispatcher is None:
            return
        self._write_endpoint(port)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dispatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._wrote_endpoint:
            path = endpoint_path(self.root)
            try:
                # Only the file this service wrote; another instance's must survive.
                if json.loads(path.read_text(encoding="utf-8")).get("token") == self.token:
                    path.unlink()
            except (OSError, ValueError):
                pass
        if self.lock is not None:
            self.lock.release()

    def health(self) -> dict[str, Any]:
        alive = self._thread is not None and self._thread.is_alive()
        return {
            "enabled": self.dispatcher is not None,
            "alive": alive,
            "seconds_since_tick": None
            if self.last_tick is None
            else round(time.monotonic() - self.last_tick, 1),
            "errors": self.errors,
            "last_error": self.last_error,
        }

    def _run(self) -> None:
        assert self.dispatcher is not None
        while not self._stop.is_set():
            try:
                self.dispatcher.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive one bad pass
                self.errors += 1
                self.last_error = f"{time.strftime('%H:%M:%S')} {exc!r}"
                traceback.print_exc()
            self.last_tick = time.monotonic()
            self._stop.wait(self.tick)

    def _write_endpoint(self, port: int) -> None:
        path = endpoint_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        # Created with its final mode, so the token is never readable by others even briefly.
        # On Windows the mode is ignored and the profile directory's ACL applies.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": self.token, "pid": os.getpid()}, f)
        tmp.replace(path)
        self._wrote_endpoint = True


class BadRequest(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _text(body: dict, key: str, low: int, high: int, default: str | None = None) -> str:
    value = body.get(key, default)
    if not isinstance(value, str) or not low <= len(value) <= high:
        raise BadRequest(400, f"{key} must be a string of {low} to {high} characters")
    return value


def _number(body: dict, key: str, default: float | None, low: float, high: float | None) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BadRequest(400, f"{key} must be a number")
    # A hold with ttl NaN would never expire and break every later status read.
    if not math.isfinite(value) or value < low or (high is not None and value > high):
        raise BadRequest(400, f"{key} is out of range")
    return float(value)


def route(service: DispatchService, method: str, path: str, query: dict, body: dict) -> dict:
    """The same routes and limits as the icc dispatcher API this replaces."""
    d = service.dispatcher
    assert d is not None

    def run[T](action: Callable[[], T]) -> T:
        try:
            return action()
        except LedgerCapacity as exc:
            raise BadRequest(507, str(exc)) from exc
        except DispatchError as exc:
            raise BadRequest(400, str(exc)) from exc

    if method == "POST" and path in (
        "send",
        "hold",
        "release",
        "await/reassign",
        "await/cancel",
        "await/close",
        "await/flag",
    ):
        _check_server(d, body)
    match method, path:
        case "POST", "send":
            client_id = body.get("client_id")
            if client_id is not None:
                client_id = _text(body, "client_id", 8, 64)
            human = body.get("human", False)
            if not isinstance(human, bool):
                raise BadRequest(400, "human must be true or false")
            target = _text(body, "target", 1, 64)
            text = _text(body, "text", 1, 20_000)
            source = _text(body, "source", 0, 64, "unknown")
            reopen = body.get("reopen", False)
            if not isinstance(reopen, bool):
                raise BadRequest(400, "reopen must be true or false")
            item = run(
                lambda: d.send_task(
                    target,
                    text,
                    source=source,
                    human=human,
                    client_id=client_id,
                    task=body.get("task"),
                    reopen=reopen,
                )
            )
            if service.mailbox:
                try:
                    mailbox.add(service.root, item)
                except OSError as exc:
                    # The mailbox is a second copy; the queued item is delivered either way.
                    print(f"courier serve: mailbox copy of {item.id} not written: {exc}")
            return {"item": asdict(item), "server": d.server}
        case "GET", "await/list":
            with d.lock:
                return {"tasks": [dict(t) for t in d.tasks.values()]}
        case "POST", "await/reassign":
            key = _text(body, "key", 1, 128)
            target = _text(body, "to", 1, 64)
            sender_pane = _text(body, "sender_pane", 1, 64)
            reason = _text(body, "reason", 0, 1000, "")
            force = body.get("force", False)
            if not isinstance(force, bool):
                raise BadRequest(400, "force must be true or false")
            return {
                "task": run(
                    lambda: d.reassign_task(
                        key,
                        target,
                        sender_pane=sender_pane,
                        force=force,
                        reason=reason,
                    )
                )
            }
        case "POST", "await/cancel":
            key = _text(body, "key", 1, 128)
            sender_pane = _text(body, "sender_pane", 1, 64)
            return {"task": run(lambda: d.cancel_task(key, sender_pane=sender_pane))}
        case "POST", "await/close":
            key = _text(body, "key", 1, 128)
            by = _text(body, "by", 1, 32)
            evidence = _text(body, "evidence", 0, 2000, "")
            limit = body.get("max_probes", 100)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise BadRequest(400, "max_probes must be an integer between 1 and 100")
            return {
                "task": run(lambda: d.close_task(key, by=by, evidence=evidence, max_probes=limit))
            }
        case "POST", "await/flag":
            key = _text(body, "key", 1, 128)
            reason = _text(body, "reason", 1, 2000)
            return {"task": run(lambda: d.flag_task(key, reason))}
        case "POST", "wait":
            item_id = _text(body, "id", 1, 32)
            timeout = _number(body, "timeout", 60, 0, 300)
            return {"item": asdict(run(lambda: d.wait(item_id, timeout)))}
        case "GET", "status":
            target = query.get("target", [None])[0]
            pane = None if target is None else run(lambda: d.resolve(target))
            return {"status": d.status(pane), "worker": service.health(), "server": d.server}
        case "POST", "hold":
            target = _text(body, "target", 1, 64)
            owner = _text(body, "owner", 0, 64, "unknown")
            reason = _text(body, "reason", 0, 200, "")
            ttl = body.get("ttl")
            if ttl is not None:
                ttl = _number(body, "ttl", None, 0, MAX_HOLD_SECONDS)
                if ttl <= 0:
                    raise BadRequest(400, "ttl must be positive")
            return {"pane_id": run(lambda: d.hold(target, owner=owner, reason=reason, ttl=ttl))}
        case "POST", "release":
            target = _text(body, "target", 1, 64)
            return {"pane_id": run(lambda: d.release(target))}
        case "POST", "cancel":
            item_id = _text(body, "id", 1, 32)
            return {"item": asdict(run(lambda: d.cancel(item_id)))}
    raise BadRequest(404, f"no route {method} /api/dispatch/{path}")


def _check_server(d: Dispatcher, body: dict) -> None:
    """Refuse a caller that runs in a pane of another herdr server: its target names and pane
    ids mean other panes there, and delivering here would reach the wrong agent."""
    claim = body.get("herdr_socket")
    if claim is None or d.server is None:
        return
    if not isinstance(claim, str) or not claim:
        raise BadRequest(400, "herdr_socket must be a non-empty string")
    if not same_socket(claim, d.server):
        raise BadRequest(
            409,
            f"this courier serve delivers to the herdr server at {d.server}; the caller is on "
            f"{claim}; nothing was done",
        )


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not allowed")


class Handler(BaseHTTPRequestHandler):
    server: "Server"
    protocol_version = "HTTP/1.1"
    # An idle client must not hold a thread forever.
    timeout = 30

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            self._reply(200, self._answer(method))
        except BadRequest as exc:
            # The body may be unread; closing keeps leftover bytes from being parsed as a request.
            self.close_connection = True
            self._reply(exc.status, {"detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 - report it instead of dropping the connection
            traceback.print_exc()
            self.close_connection = True
            self._reply(500, {"detail": f"internal error: {exc!r}"})

    def _answer(self, method: str) -> dict:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            # A page on another site that resolves its name to 127.0.0.1 must not reach us.
            raise BadRequest(400, "invalid host")
        url = urlsplit(self.path)
        if not url.path.startswith("/api/dispatch/"):
            raise BadRequest(404, "not found")
        service = self.server.service
        if service.dispatcher is None:
            raise BadRequest(503, "dispatch is turned off")
        given = (self.headers.get("Authorization") or "").encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(given, f"Bearer {service.token}".encode()):
            raise BadRequest(401, "missing or wrong dispatch token")
        body: dict = {}
        if method == "POST":
            if self.headers.get("Transfer-Encoding"):
                raise BadRequest(411, "send a Content-Length body, not chunked")
            raw_length = self.headers.get("Content-Length") or "0"
            if not raw_length.isdigit():
                raise BadRequest(400, "invalid Content-Length")
            length = int(raw_length)
            if length > MAX_BODY:
                raise BadRequest(413, "request body too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}", parse_constant=_reject_constant)
            except ValueError as exc:
                raise BadRequest(400, "body is not JSON") from exc
            if not isinstance(body, dict):
                raise BadRequest(400, "send a JSON object")
        path = url.path.removeprefix("/api/dispatch/")
        return route(service, method, path, parse_qs(url.query), body)

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps({**payload, "api": API_VERSION}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, service: DispatchService, port: int = 0) -> None:
        self.service = service
        super().__init__(("127.0.0.1", port), Handler)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (0: any)")
    parser.add_argument("--tick", type=float, default=TICK, help="seconds between passes")
    parser.add_argument(
        "--transport",
        choices=["agent", "pane"],
        default="agent",
        help="agent: herdr agent prompt (default); pane: send-text + Enter, for test dummies",
    )


def serve(args: argparse.Namespace) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True, errors="replace")

    try:
        if old := adopt_old_state():
            print(f"courier serve: moved the state directory of herdr-console 0.1 from {old}")
    except OSError as exc:
        print(
            f"courier serve: cannot move the old state directory ({exc}); stop hcd 0.1 first",
            file=sys.stderr,
        )
        return 1
    root = state_dir()
    try:
        # Taken before the journal is read, so a second instance never touches it.
        lock = InstanceLock(lock_path(root))
    except AlreadyRunning as exc:
        print(f"courier serve: {exc}", file=sys.stderr)
        return 1
    try:
        config = load(config_path(root))
    except ConfigError as exc:
        lock.release()
        print(f"courier serve: {exc}", file=sys.stderr)
        return 1
    herdr = PaneHerdr() if args.transport == "pane" else CliHerdr()
    settle_for = ovis.settle_rule(
        ovis.Presence(config.ovis.url), config.ovis.panes, config.ovis.settle
    )
    try:
        herdr_socket = herdr.server_socket()
        dispatcher = Dispatcher(
            herdr,
            journal_path(root),
            settle_for=settle_for,
            server=herdr_socket,
            hold_missing=config.hold.when_missing,
            hold_ttl=config.hold.ttl,
            summary_to=config.hold.summary_to,
            summary_every=config.hold.summary_every,
            controller=config.ledger.controller,
            secretary=config.ledger.secretary,
        )
    except (HerdrError, WrongServer) as exc:
        lock.release()
        print(f"courier serve: {exc}", file=sys.stderr)
        return 1
    service = DispatchService(dispatcher, root, args.tick, lock, config.mailbox.write)
    server = Server(service, args.port)
    port = server.server_address[1]
    service.start(port)
    print(
        f"courier serve pid {os.getpid()} on 127.0.0.1:{port}, "
        f"transport {args.transport}, state {root}, herdr {herdr_socket}"
    )
    if config.ovis.url and config.ovis.panes:
        print(
            f"between turns only, while Ovis answers at {config.ovis.url}: "
            f"{', '.join(config.ovis.panes)} (settle {config.ovis.settle:g} s)"
        )
    if config.hold.when_missing:
        print(
            f"held while missing, up to {config.hold.ttl / 3600:g} h: "
            f"{', '.join(config.hold.when_missing)}"
        )
    if config.mailbox.write:
        print(f"mailbox copy of every queued message in {mailbox.mailbox_dir(root)}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()
        print(
            "courier serve stopped; queued items stay in the journal and resume on the next start"
        )
    return 0
