"""hcd: the single process that writes prompts into herdr panes.

Run it in a herdr shell pane, not inside an agent's background shell, so it outlives agent
sessions. It listens on 127.0.0.1 only and requires the token from its endpoint file, which
lives in the user's local state directory.
"""

import argparse
import hmac
import io
import json
import os
import secrets
import socketserver
import sys
import threading
import time
from dataclasses import asdict
from typing import Any

from .dispatcher import Dispatcher, DispatchError
from .herdr import CliHerdr, PaneHerdr
from .wire import Unavailable, endpoint_file, request, state_dir

TICK = 0.5


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def handle(dispatcher: Dispatcher, msg: dict[str, Any]) -> dict[str, Any]:
    op = msg.get("op")
    try:
        match op:
            case "ping":
                return {"ok": True, "pid": os.getpid()}
            case "send":
                item = dispatcher.enqueue(
                    msg["target"],
                    msg["text"],
                    source=msg.get("source", "?"),
                    human=bool(msg.get("human")),
                )
                return {"ok": True, "item": asdict(item)}
            case "wait":
                item = dispatcher.wait(msg["id"], float(msg.get("timeout", 60)))
                return {"ok": True, "item": asdict(item)}
            case "status":
                return {"ok": True, "status": dispatcher.status()}
            case "hold":
                pane = dispatcher.hold(
                    msg["target"],
                    owner=msg.get("owner", "?"),
                    reason=msg.get("reason", ""),
                    ttl=msg.get("ttl"),
                )
                return {"ok": True, "pane_id": pane}
            case "release":
                return {"ok": True, "pane_id": dispatcher.release(msg["target"])}
            case "cancel":
                return {"ok": True, "item": asdict(dispatcher.cancel(msg["id"]))}
            case _:
                return {"ok": False, "error": f"unknown op {op!r}"}
    except (DispatchError, KeyError, TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def serve(dispatcher: Dispatcher, token: str, port: int = 0) -> Server:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline(1 << 20)
            try:
                msg = json.loads(line)
            except ValueError:
                return
            if not hmac.compare_digest(str(msg.get("token", "")), token):
                reply = {"ok": False, "error": "bad token"}
            else:
                reply = handle(dispatcher, msg)
            self.wfile.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\n")

    return Server(("127.0.0.1", port), Handler)


def run_worker(dispatcher: Dispatcher, stop: threading.Event, tick: float = TICK) -> None:
    while not stop.is_set():
        dispatcher.tick()
        stop.wait(tick)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hcd", description="Prompt dispatcher for herdr panes.")
    parser.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (0: any)")
    parser.add_argument("--tick", type=float, default=TICK, help="seconds between passes")
    parser.add_argument(
        "--transport",
        choices=["agent", "pane"],
        default="agent",
        help="agent: herdr agent prompt (default); pane: send-text + Enter, for test dummies",
    )
    args = parser.parse_args(argv)

    try:
        reply = request("ping", timeout=2)
        if reply.get("ok"):
            print(
                f"hcd already running (pid {reply['pid']}); not starting another", file=sys.stderr
            )
            return 1
    except Unavailable:
        pass

    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    herdr = PaneHerdr() if args.transport == "pane" else CliHerdr()
    dispatcher = Dispatcher(herdr, state_dir() / "journal.jsonl")
    token = secrets.token_urlsafe(24)
    server = serve(dispatcher, token, args.port)
    port = server.server_address[1]
    write_endpoint({"port": port, "token": token, "pid": os.getpid(), "started": time.time()})
    stop = threading.Event()
    worker = threading.Thread(target=run_worker, args=(dispatcher, stop, args.tick), daemon=True)
    worker.start()
    print(f"hcd pid {os.getpid()} on 127.0.0.1:{port}, transport {args.transport}")
    print(f"state {state_dir()}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        remove_endpoint(os.getpid())
        print("hcd stopped; queued items stay in the journal and resume on next start")
    return 0


def write_endpoint(data: dict[str, Any]) -> None:
    path = endpoint_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def remove_endpoint(pid: int) -> None:
    path = endpoint_file()
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == pid:
            path.unlink()
    except (OSError, ValueError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
