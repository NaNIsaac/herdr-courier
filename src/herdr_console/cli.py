"""hc: queue prompts through hcd instead of writing into panes directly.

``hc send`` takes the same arguments as ``herdr agent prompt``. Without ``--wait`` it returns
once hcd has queued the message; with ``--wait`` it waits for delivery and then, like
``herdr agent prompt --wait``, for the agent to settle.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import time
from typing import Any

from .wire import LostContact, Unavailable, request

EXIT_FAILED = 1
EXIT_UNAVAILABLE = 3
EXIT_TIMEOUT = 4


def manual_path(target: str, text: str | None) -> str:
    shown = "<text>" if text is None else json.dumps(text, ensure_ascii=False)
    return (
        "Nothing was sent. Start hcd in a herdr shell pane (see the README), or deliver by hand:\n"
        f"  herdr agent read {target} --source detection   # the line after the last prompt "
        "glyph must be empty\n"
        f"  herdr agent prompt {target} {shown}"
    )


def call(op: str, params: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    params = params or {}
    try:
        reply = request(op, params, timeout)
    except Unavailable as exc:
        print(f"hc: hcd is not running ({exc}).", file=sys.stderr)
        print(manual_path(params.get("target", "<target>"), params.get("text")), file=sys.stderr)
        raise SystemExit(EXIT_UNAVAILABLE) from exc
    except LostContact as exc:
        print(f"hc: {exc}. It may have been applied; check hc status.", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from exc
    if not reply.get("ok"):
        print(f"hc: {reply.get('error')}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED)
    return reply


def default_source() -> str:
    return os.environ.get("HC_SOURCE") or os.environ.get("HERDR_PANE_ID") or "unknown"


def cmd_send(args: argparse.Namespace) -> int:
    started = time.monotonic()
    reply = call(
        "send",
        {"target": args.target, "text": args.text, "source": args.source, "human": args.human},
    )
    item = reply["item"]
    if not args.wait:
        print(f"queued {item['id']} for {item['target']} ({item['pane_id']})")
        return 0
    budget = None if args.timeout is None else args.timeout / 1000
    while True:
        left = 60.0 if budget is None else budget - (time.monotonic() - started)
        if left <= 0:
            print(f"timeout: {item['id']} is {item['state']}: {item['reason']}", file=sys.stderr)
            return EXIT_TIMEOUT
        step = min(left, 60.0)
        item = call("wait", {"id": item["id"], "timeout": step}, timeout=step + 5)["item"]
        if item["state"] in ("sent", "failed", "cancelled"):
            break
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


def cmd_status(args: argparse.Namespace) -> int:
    status = call("status")["status"]
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    now = time.time()
    panes = status["panes"]
    if args.target:
        panes = {
            k: v
            for k, v in panes.items()
            if args.target in (k, *(i["target"] for i in v["pending"]))
        }
    if not panes:
        print("no queued messages or holds")
    for pane_id, info in panes.items():
        hold = info["hold"]
        held = "" if hold is None else f"  HOLD {hold['reason']} ({hold['owner']}"
        if hold is not None:
            held += ")" if hold["seconds_left"] is None else f", {hold['seconds_left']} s left)"
        print(f"{pane_id}{held}")
        if info["observed"]:
            print(f"  observed: {info['observed']}")
        for item in info["pending"]:
            age = int(now - item["created"])
            who = "human " if item["human"] else ""
            print(
                f"  {item['id']} {item['state']:9} {age:4d}s {who}from {item['source']}: "
                f"{item['reason']}"
            )
            print(f"      {preview(item['text'])}")
    if status["recent"] and not args.target:
        print("recent:")
        for item in status["recent"][-args.recent :]:
            stamp = time.strftime("%H:%M:%S", time.localtime(item["updated"]))
            print(
                f"  {stamp} {item['id']} {item['state']:9} {item['target']}: {item['reason']}"
                f"{f' (attempts {item["attempts"]})' if item['attempts'] else ''}"
            )
    return 0


def preview(text: str, width: int = 70) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def cmd_hold(args: argparse.Namespace) -> int:
    reply = call(
        "hold",
        {"target": args.target, "owner": args.owner, "reason": args.reason, "ttl": args.ttl},
    )
    span = "until hc release" if args.ttl is None else f"for {args.ttl:g} s"
    print(f"holding {reply['pane_id']} {span}")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    print(f"released {call('release', {'target': args.target})['pane_id']}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    item = call("cancel", {"id": args.id})["item"]
    print(f"cancelled {item['id']} to {item['target']}")
    return 0


def cmd_ping(_: argparse.Namespace) -> int:
    print(f"hcd running, pid {call('ping')['pid']}")
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

    p = sub.add_parser("status", help="queues, holds and what hcd last saw in each pane")
    p.add_argument("target", nargs="?")
    p.add_argument("--json", action="store_true")
    p.add_argument("--recent", type=int, default=10, help="finished items to show")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("hold", help="keep hcd from writing into a pane")
    p.add_argument("target")
    p.add_argument("--ttl", type=float, metavar="SECONDS", help="expire automatically")
    p.add_argument("--reason", default="")
    p.add_argument("--owner", default=default_source())
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("release", help="drop a hold")
    p.add_argument("target")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("cancel", help="drop a queued message")
    p.add_argument("id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("ping", help="check that hcd is running")
    p.set_defaults(func=cmd_ping)

    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
