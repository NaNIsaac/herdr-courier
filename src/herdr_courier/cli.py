"""courier: queue prompts through courier serve instead of writing into panes directly.

``courier send`` takes the same arguments as ``herdr agent prompt``. Without ``--wait`` it returns
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
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

from . import agents, config, daemon, display, doctor, mailbox, panes
from .client import LostContact, Refused, Unavailable, request
from .core.transport import CliHerdr, HerdrError
from .state import state_dir

EXIT_FAILED = 1
EXIT_UNAVAILABLE = 3
EXIT_TIMEOUT = 4
# Longer than the server's worst case for resolving a target (herdr CLI timeout 15 s).
SEND_TIMEOUT = 30.0
SERVE_HELP = "run the dispatcher, the one process that writes into panes"
SERVE_DESCRIPTION = "Run the dispatcher in the foreground. Start it in a herdr shell pane."
# What Git Bash makes of an argument that starts with /, such as /effort (MSYS path conversion).
GIT_BASH_PREFIX = "C:/Program Files/Git/"


def manual_path(target: str, text: str | None) -> str:
    shown = "<text>" if text is None else json.dumps(text, ensure_ascii=False)
    return (
        "Nothing was sent. Start courier serve in a herdr shell pane, or deliver by hand:\n"
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
            print(f"courier: dispatcher unavailable ({exc}).", file=sys.stderr)
            body = body or {}
            print(manual_path(body.get("target", "<target>"), body.get("text")), file=sys.stderr)
            raise SystemExit(EXIT_UNAVAILABLE) from exc
        case LostContact():
            print(
                f"courier: {exc}. It may have been applied; check courier status.", file=sys.stderr
            )
        case _:
            print(f"courier: {exc}", file=sys.stderr)
    raise SystemExit(EXIT_FAILED) from exc


def on_server(body: dict[str, Any]) -> dict[str, Any]:
    """`body` with the herdr server of the calling pane, which courier serve checks against its own.

    A caller outside herdr names none and is taken to mean courier serve's server.
    """
    if socket := os.environ.get("HERDR_SOCKET_PATH"):
        return body | {"herdr_socket": socket}
    return body


def default_source() -> str:
    return os.environ.get("COURIER_SOURCE") or os.environ.get("HERDR_PANE_ID") or "unknown"


def cmd_send(args: argparse.Namespace) -> int:
    started = time.monotonic()
    if args.text.startswith(GIT_BASH_PREFIX):
        print(
            f"courier: warning: the text starts with {GIT_BASH_PREFIX}, which is how Git Bash "
            "rewrites a leading /; run with MSYS_NO_PATHCONV=1 if you meant a slash command",
            file=sys.stderr,
        )
    body = on_server(
        {
            "target": args.target,
            "text": args.text,
            "source": args.source,
            "human": args.human,
            # Lets a retry after a lost reply find the first request instead of queuing twice.
            "client_id": uuid.uuid4().hex,
        }
    )
    try:
        reply = request("POST", "send", body, timeout=SEND_TIMEOUT)
    except LostContact:
        reply = call("POST", "send", body, timeout=SEND_TIMEOUT)
    except (Unavailable, Refused) as exc:
        fail(exc, body)
    item = reply["item"]
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
    # The pane id is courier serve's, so ask the herdr server it delivered to.
    env = os.environ | ({"HERDR_SOCKET_PATH": reply["server"]} if reply.get("server") else {})
    return subprocess.run(wait, check=False, env=env).returncode


def render_status(reply: dict[str, Any], *, width: int, recent: int, now: float) -> list[str]:
    """``courier status`` as lines of at most ``width`` cells, so a phone shows it without wrapping."""
    status, worker = reply["status"], reply.get("worker") or {}
    out: list[str] = []
    stale = (worker.get("seconds_since_tick") or 0) > 10
    if not worker.get("alive") or stale:
        out += display.wrap(
            f"WARNING: dispatcher worker is not running (alive={worker.get('alive')}, "
            f"last tick {worker.get('seconds_since_tick')} s ago); queued messages will not go out",
            width,
            "",
            "  ",
        )
    if worker.get("last_error"):
        out += display.wrap(
            f"last worker error ({worker.get('errors')} so far): {worker['last_error']}",
            width,
            "",
            "  ",
        )
    panes = status["panes"]
    if not any(v["pending"] or v["hold"] for v in panes.values()):
        out.append("no queued messages or holds")
    for pane_id, info in panes.items():
        hold = info["hold"]
        if hold is None:
            out.append(pane_id)
        else:
            left = "" if hold["seconds_left"] is None else f", {hold['seconds_left']} s left"
            again = ", restored after restart" if hold.get("restored") else ""
            out += display.wrap(
                f"HOLD {hold['reason']} ({hold['owner']}{left}{again})",
                width,
                f"{pane_id}  ",
                "    ",
            )
        if info["observed"]:
            out += display.wrap(info["observed"], width, "  observed: ", "    ")
        for item in info["pending"]:
            who = "human " if item["human"] else ""
            out += display.wrap(
                f"{who}from {item['source']}: {item['reason']}",
                width,
                f"  {item['id']} {item['state']:9} {int(now - item['created']):4d}s ",
                "      ",
            )
            out.append("      " + display.clip(item["text"], width - 6))
    if status["recent"]:
        out.append("recent:")
        for item in status["recent"][-recent:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(item["updated"]))
            tries = f" (attempts {item['attempts']})" if item["attempts"] else ""
            prefix = f"  {stamp} {item['id']} {item['state']:9} "
            detail = f"{item['target']}: {item['reason']}{tries}"
            # A delivered item's reason only says how delivery was confirmed; one line each keeps
            # the log short on a phone. Any other outcome is kept whole, since its reason is the point.
            if item["state"] == "sent":
                out.append(display.fit(prefix + detail, width).rstrip())
            else:
                out += display.wrap(detail, width, prefix, "    ")
    return out


def cmd_status(args: argparse.Namespace) -> int:
    path = (
        "status" if not args.target else "status?" + urllib.parse.urlencode({"target": args.target})
    )
    reply = call("GET", path)
    if args.json:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
        return 0
    width = args.width or display.terminal_width()
    print("\n".join(render_status(reply, width=width, recent=args.recent, now=time.time())))
    return 0


def cmd_hold(args: argparse.Namespace) -> int:
    body = on_server(
        {"target": args.target, "owner": args.owner, "reason": args.reason, "ttl": args.ttl}
    )
    pane = call("POST", "hold", body)["pane_id"]
    print(
        f"holding {pane} "
        + ("until courier release" if args.ttl is None else f"for {args.ttl:g} s")
    )
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    print(f"released {call('POST', 'release', on_server({'target': args.target}))['pane_id']}")
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    try:
        reply = request("GET", "status")
    except Unavailable as exc:
        dispatch, summary = None, f"not running ({exc}); courier send would fail with status 3"
    except (LostContact, Refused) as exc:
        dispatch, summary = None, f"error ({exc})"
    else:
        dispatch = reply["status"]
        worker = reply.get("worker") or {}
        queued = sum(len(p["pending"]) for p in dispatch["panes"].values())
        held = sum(1 for p in dispatch["panes"].values() if p["hold"])
        summary = (
            f"running, {queued} queued, {held} held"
            if worker.get("alive")
            else "answers but its worker is not running; nothing is being sent"
        )
    try:
        rows = agents.collect(CliHerdr(), dispatch)
    except HerdrError as exc:
        print(f"courier: herdr did not answer ({exc})", file=sys.stderr)
        return EXIT_FAILED
    notes = agents.warnings(rows)
    if args.json:
        print(
            json.dumps(
                {"dispatcher": summary, "rows": [r.as_dict() for r in rows], "warnings": notes},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    width = args.width or display.terminal_width()
    color = display.color_wanted(args.color, sys.stdout)
    print(agents.render(rows, notes, width=width, dispatcher=summary, wide=args.wide, color=color))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor.diagnose(doctor.LiveProbes())
    if args.json:
        print(json.dumps([c.as_dict() for c in checks], ensure_ascii=False, indent=2))
    else:
        print(doctor.render(checks, args.width or display.terminal_width()))
    return EXIT_FAILED if any(c.level is doctor.Level.FAIL for c in checks) else 0


def cmd_panes(args: argparse.Namespace) -> int:
    try:
        rule = config.load().panes
    except config.ConfigError as exc:
        print(f"courier: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        tabs = panes.collect(CliHerdr())
    except HerdrError as exc:
        print(f"courier: herdr did not answer ({exc})", file=sys.stderr)
        return EXIT_FAILED
    notes = panes.warnings(tabs, rule)
    if args.json:
        payload = {"tabs": [t.as_dict() for t in tabs], "warnings": notes}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(panes.render(tabs, notes, width=args.width or display.terminal_width(), rule=rule))
    return 0


def own_name() -> str | None:
    """The herdr agent name of the calling pane, as role-brief finds it."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return None
    try:
        found = [a for a in CliHerdr().agent_list() if a.get("pane_id") == pane]
    except HerdrError:
        return None
    return (found[0].get("name") or None) if found else None


def inbox_hook(root: Path, name: str | None) -> int:
    """For a Claude Code hook: open messages as additionalContext, and nothing when there are none.

    UserPromptSubmit shows new messages; SessionStart (after a compaction or a restart) shows every
    message not yet marked done again, since the context that held them may be gone. A hook must
    not break the agent's turn, so every failure is logged and the exit status stays 0.
    """
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        payload = json.loads(raw) if raw.strip() else {}
        event = payload.get("hook_event_name") or "UserPromptSubmit"
        detail = f"{event}/{payload['source']}" if payload.get("source") else event
        readers = config.load().mailbox.readers
        # Checked before asking herdr, so the hook costs nothing in a store with no readers.
        if not readers or (name := name or own_name()) not in readers:
            return 0
        messages = mailbox.pending(mailbox.load(root, name), again=event == "SessionStart")
        text = mailbox.hook_text(name, messages) if messages else ""
        ids = ",".join(m.id for m in messages) or "-"
        mailbox.log_hook(root, f"{name} {detail} shown={ids} bytes={len(text.encode())}")
        if not messages:
            return 0
        out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
        print(json.dumps(out, ensure_ascii=False))
        mailbox.mark(root, name, [m.id for m in messages], "shown", detail)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(f"courier inbox --hook: {exc!r}", file=sys.stderr)
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    root = state_dir()
    if args.hook:
        return inbox_hook(root, args.name)
    name = args.name or own_name()
    if name is None:
        print("courier: no agent name for this pane; pass one: courier inbox NAME", file=sys.stderr)
        return EXIT_FAILED
    messages = mailbox.load(root, name)
    by_id = {m.id: m for m in messages}
    if unknown := [i for i in args.ack + args.show if i not in by_id]:
        print(f"courier: no message {unknown[0]} in the mailbox of {name}", file=sys.stderr)
        return EXIT_FAILED
    if args.ack:
        mailbox.mark(root, name, [i for i in args.ack if by_id[i].state != "done"], "done")
        print(f"done {' '.join(args.ack)}")
        return 0
    chosen = (
        [by_id[i] for i in args.show]
        if args.show
        else [m for m in messages if args.all or m.state != "done"]
    )
    if args.json:
        print(
            json.dumps(
                [asdict(m) | {"state": m.state} for m in chosen], ensure_ascii=False, indent=2
            )
        )
        return 0
    if not chosen:
        print(f"no open messages for {name}")
    for m in chosen:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.created))
        print(f"--- {m.id} from {m.source} at {stamp} ({m.state}) ---\n{m.text}\n")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    item = call("POST", "cancel", {"id": args.id})["item"]
    print(f"cancelled {item['id']} to {item['target']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="courier", description="Queue prompts for herdr panes through courier serve."
    )
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
    p.add_argument("--width", type=int, help="line width (default: the terminal, 62 if unknown)")
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

    p = sub.add_parser(
        "agents", help="every pane with its tab, agent name, status and dispatcher queue"
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--width", type=int, help="table width (default: the terminal, 62 if unknown)")
    p.add_argument("--wide", action="store_true", help="add the terminal title column")
    p.add_argument(
        "--color",
        nargs="?",
        const="always",
        default="auto",
        choices=display.COLOR_CHOICES,
        metavar="WHEN",
        help="auto (default: a terminal, unless NO_COLOR is set), always (also bare --color), never",
    )
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser(
        "doctor",
        help="check herdr, courier serve, its journal and config, other pane writers and Ovis",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--width", type=int, help="line width (default: the terminal, 62 if unknown)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("panes", help="panes by tab with label and working directory")
    p.add_argument("--json", action="store_true")
    p.add_argument("--width", type=int, help="line width (default: the terminal, 62 if unknown)")
    p.set_defaults(func=cmd_panes)

    p = sub.add_parser("inbox", help="messages in an agent's mailbox, and marking them done")
    p.add_argument("name", nargs="?", help="agent name (default: this pane's herdr agent name)")
    p.add_argument("--ack", nargs="+", default=[], metavar="ID", help="mark messages done")
    p.add_argument("--show", nargs="+", default=[], metavar="ID", help="print these in full")
    p.add_argument("--all", action="store_true", help="include messages already done")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--hook",
        action="store_true",
        help="read a Claude Code hook payload on stdin and answer with open messages",
    )
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("cancel", help="drop a queued message")
    p.add_argument("id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("serve", help=SERVE_HELP, description=SERVE_DESCRIPTION)
    daemon.add_arguments(p)
    p.set_defaults(func=daemon.serve)

    args = parser.parse_args(argv)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    return args.func(args)


def serve_main(argv: list[str] | None = None) -> int:
    """Entry point of ``hcd``, the same dispatcher as ``courier serve``."""
    parser = argparse.ArgumentParser(prog="hcd", description=SERVE_DESCRIPTION)
    daemon.add_arguments(parser)
    return daemon.serve(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
