"""hc: queue prompts through hcd instead of writing into panes directly.

``hc send`` takes the same arguments as ``herdr agent prompt``. Without ``--wait`` it returns
once the message is queued; with ``--wait`` it waits for delivery and then, like
``herdr agent prompt --wait``, for the agent to settle.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
import uuid
from typing import Any, NoReturn

from . import agents
from .client import LostContact, Refused, Unavailable, request
from .core.transport import CliHerdr, HerdrError

EXIT_FAILED = 1
EXIT_UNAVAILABLE = 3
EXIT_TIMEOUT = 4
# Longer than the server's worst case for resolving a target (herdr CLI timeout 15 s).
SEND_TIMEOUT = 30.0


def manual_path(target: str, text: str | None) -> str:
    shown = "<text>" if text is None else json.dumps(text, ensure_ascii=False)
    return (
        "Nothing was sent. Start hcd in a herdr shell pane, or deliver by hand:\n"
        f"  herdr agent read {target} --source recent --lines 12 --format ansi\n"
        "  # in the last input box, the text after the prompt glyph must be empty or dim\n"
        f"  herdr agent prompt {target} {shown}"
    )


def call(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0):
    try:
        return request(method, path, body, timeout)
    except (Unavailable, LostContact, Refused) as exc:
        fail(exc, body)


def fail(exc: Exception, body: dict[str, Any] | None) -> NoReturn:
    match exc:
        case Unavailable():
            print(f"hc: dispatcher unavailable ({exc}).", file=sys.stderr)
            body = body or {}
            print(manual_path(body.get("target", "<target>"), body.get("text")), file=sys.stderr)
            raise SystemExit(EXIT_UNAVAILABLE) from exc
        case LostContact():
            print(f"hc: {exc}. It may have been applied; check hc status.", file=sys.stderr)
        case _:
            print(f"hc: {exc}", file=sys.stderr)
    raise SystemExit(EXIT_FAILED) from exc


def default_source() -> str:
    return os.environ.get("HC_SOURCE") or os.environ.get("HERDR_PANE_ID") or "unknown"


def cmd_send(args: argparse.Namespace) -> int:
    started = time.monotonic()
    body = {
        "target": args.target,
        "text": args.text,
        "source": args.source,
        "human": args.human,
        # Lets a retry after a lost reply find the first request instead of queuing twice.
        "client_id": uuid.uuid4().hex,
    }
    try:
        item = request("POST", "send", body, timeout=SEND_TIMEOUT)["item"]
    except LostContact:
        item = call("POST", "send", body, timeout=SEND_TIMEOUT)["item"]
    except (Unavailable, Refused) as exc:
        fail(exc, body)
    if not args.wait:
        print(f"queued {item['id']} for {item['target']} ({item['pane_id']})")
        return 0
    budget = None if args.timeout is None else args.timeout / 1000
    while item["state"] not in ("sent", "failed", "cancelled"):
        left = 60.0 if budget is None else budget - (time.monotonic() - started)
        if left <= 0:
            print(f"timeout: {item['id']} is {item['state']}: {item['reason']}", file=sys.stderr)
            return EXIT_TIMEOUT
        step = min(left, 60.0)
        item = call("POST", "wait", {"id": item["id"], "timeout": step}, timeout=step + 5)["item"]
    print(f"{item['state']} {item['id']} to {item['target']} ({item['pane_id']}): {item['reason']}")
    if item["state"] != "sent":
        return EXIT_FAILED
    wait = ["herdr", "agent", "wait", item["pane_id"]]
    for status in args.until or []:
        wait += ["--until", status]
    if budget is not None:
        left_ms = int((budget - (time.monotonic() - started)) * 1000)
        if left_ms <= 0:
            return EXIT_TIMEOUT
        wait += ["--timeout", str(left_ms)]
    return subprocess.run(wait, check=False).returncode


def preview(text: str, width: int = 70) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def cmd_status(args: argparse.Namespace) -> int:
    path = (
        "status" if not args.target else "status?" + urllib.parse.urlencode({"target": args.target})
    )
    reply = call("GET", path)
    status, worker = reply["status"], reply.get("worker") or {}
    if args.json:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
        return 0
    stale = (worker.get("seconds_since_tick") or 0) > 10
    if not worker.get("alive") or stale:
        print(
            f"WARNING: dispatcher worker is not running (alive={worker.get('alive')}, "
            f"last tick {worker.get('seconds_since_tick')} s ago); queued messages will not go out"
        )
    if worker.get("last_error"):
        print(f"last worker error ({worker.get('errors')} so far): {worker['last_error']}")
    now = time.time()
    panes = status["panes"]
    if not any(v["pending"] or v["hold"] for v in panes.values()):
        print("no queued messages or holds")
    for pane_id, info in panes.items():
        hold = info["hold"]
        line = pane_id
        if hold is not None:
            left = "" if hold["seconds_left"] is None else f", {hold['seconds_left']} s left"
            again = ", restored after restart" if hold.get("restored") else ""
            line += f"  HOLD {hold['reason']} ({hold['owner']}{left}{again})"
        print(line)
        if info["observed"]:
            print(f"  observed: {info['observed']}")
        for item in info["pending"]:
            who = "human " if item["human"] else ""
            print(
                f"  {item['id']} {item['state']:9} {int(now - item['created']):4d}s "
                f"{who}from {item['source']}: {item['reason']}"
            )
            print(f"      {preview(item['text'])}")
    if status["recent"]:
        print("recent:")
        for item in status["recent"][-args.recent :]:
            stamp = time.strftime("%H:%M:%S", time.localtime(item["updated"]))
            tries = f" (attempts {item['attempts']})" if item["attempts"] else ""
            print(
                f"  {stamp} {item['id']} {item['state']:9} {item['target']}: {item['reason']}{tries}"
            )
    return 0


def cmd_hold(args: argparse.Namespace) -> int:
    body = {"target": args.target, "owner": args.owner, "reason": args.reason, "ttl": args.ttl}
    pane = call("POST", "hold", body)["pane_id"]
    print(f"holding {pane} " + ("until hc release" if args.ttl is None else f"for {args.ttl:g} s"))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    print(f"released {call('POST', 'release', {'target': args.target})['pane_id']}")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    try:
        reply = request("GET", "status")
    except Unavailable as exc:
        dispatch, hcd = None, f"not running ({exc}); hc send would fail with status 3"
    except (LostContact, Refused) as exc:
        dispatch, hcd = None, f"error ({exc})"
    else:
        dispatch = reply["status"]
        worker = reply.get("worker") or {}
        queued = sum(len(p["pending"]) for p in dispatch["panes"].values())
        held = sum(1 for p in dispatch["panes"].values() if p["hold"])
        hcd = (
            f"running, {queued} queued, {held} held"
            if worker.get("alive")
            else "answers but its worker is not running; nothing is being sent"
        )
    try:
        rows = agents.collect(CliHerdr(), dispatch)
    except HerdrError as exc:
        print(f"hc: herdr did not answer ({exc})", file=sys.stderr)
        return EXIT_FAILED
    notes = agents.warnings(rows)
    if args.json:
        print(
            json.dumps(
                {"hcd": hcd, "rows": [r.as_dict() for r in rows], "warnings": notes},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    width = args.width or agents.terminal_width()
    print(agents.render(rows, notes, width=width, hcd=hcd, wide=args.wide))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    item = call("POST", "cancel", {"id": args.id})["item"]
    print(f"cancelled {item['id']} to {item['target']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hc", description="Queue prompts through hcd.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("send", help="queue a prompt (same arguments as herdr agent prompt)")
    p.add_argument("target", help="agent name or pane id")
    p.add_argument("text")
    p.add_argument("--wait", action="store_true", help="wait for delivery, then for the agent")
    p.add_argument("--until", action="append", metavar="STATUS", help="passed to herdr agent wait")
    p.add_argument("--timeout", type=int, metavar="MS", help="total budget for --wait")
    p.add_argument("--source", default=default_source(), help="who is sending (default: pane)")
    p.add_argument("--human", action="store_true", help="put ahead of queued agent messages")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("status", help="queues, holds and what the dispatcher last saw")
    p.add_argument("target", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--recent", type=int, default=10, help="finished items to show")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("hold", help="keep the dispatcher from writing into a pane")
    p.add_argument("target")
    p.add_argument("--ttl", type=float, metavar="SECONDS", help="expire automatically")
    p.add_argument("--reason", default="")
    p.add_argument("--owner", default=default_source())
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("release", help="drop a hold")
    p.add_argument("target")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("agents", help="every pane with its tab, agent name, status and hcd queue")
    p.add_argument("--json", action="store_true")
    p.add_argument("--width", type=int, help="table width (default: the terminal, 62 if unknown)")
    p.add_argument("--wide", action="store_true", help="add the terminal title column")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("cancel", help="drop a queued message")
    p.add_argument("id")
    p.set_defaults(func=cmd_cancel)

    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
