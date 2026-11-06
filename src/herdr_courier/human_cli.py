"""Human-facing question providers and messages, installed as pepperpaw and ppw."""

import argparse
import json
import sys
from pathlib import Path

from . import cli, config, display, questions
from .core.transport import CliHerdr, HerdrError


def print_text(text: str, width: int) -> None:
    for line in text.replace("\t", "    ").split("\n"):
        current, used = "", 0
        for character in line:
            if ord(character) < 32 or 127 <= ord(character) < 160:
                character = "�"
            size = display.cols(character)
            if used + size > width:
                print(current)
                current, used = "", 0
            current += character
            used += size
        print(current)


def qualified(adapters, value: str):
    name, separator, key = value.partition(":")
    if not separator or not key or name not in adapters:
        raise questions.ProviderError("use an exact configured provider:id")
    return name, adapters[name], key


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False))


def where(herdr, name: str, *, focus: bool, separator: str) -> int:
    tabs = {tab["tab_id"]: tab for tab in herdr.tab_list()}
    agents = {agent["pane_id"]: agent for agent in herdr.agent_list()}
    found = []
    for pane in herdr.pane_list():
        label = pane.get("label") or ""
        service = label.partition(separator)[2] if separator else ""
        agent = agents.get(pane["pane_id"], {})
        if name and name in (agent.get("name"), label, service, pane["pane_id"]):
            found.append(pane)
    if not found:
        raise questions.ProviderError(f"no pane matches {name!r}")
    if len(found) != 1:
        ids = ", ".join(pane["pane_id"] for pane in found)
        raise questions.ProviderError(
            f"ambiguous name {name!r}: {ids}; use a full label or pane id"
        )
    pane = found[0]
    tab_id = pane["tab_id"]
    if focus:
        if pane["pane_id"] in agents:
            herdr.agent_focus(pane["pane_id"])
        else:
            herdr.tab_focus(tab_id)
    line = ", ".join(
        (
            tabs.get(tab_id, {}).get("label") or tab_id,
            pane.get("label") or "(unlabelled)",
            pane["pane_id"],
        )
    )
    print("".join(" " if c.isspace() or ord(c) < 32 or 127 <= ord(c) < 160 else c for c in line))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ppw", description="Read and answer questions, send messages, or locate panes."
    )
    commands = result.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("q", help="list questions, or view one provider:id")
    listing.add_argument("id", nargs="?")
    listing.add_argument("--all", action="store_true", help="include questions that are not open")
    answering = commands.add_parser("a", aliases=["answer"], help="answer one provider:id")
    answering.add_argument("id")
    text = answering.add_mutually_exclusive_group(required=True)
    text.add_argument("text", nargs="?")
    text.add_argument("--file", type=Path, help="read the answer as UTF-8 text")
    sending = commands.add_parser("send", help="send with the configured human identity")
    sending.add_argument("text", nargs="?", default="")
    sending.add_argument("--to")
    sending.add_argument("--file", action="append", default=[])
    locating = commands.add_parser("where", help="locate an agent, pane label or service label")
    locating.add_argument("name")
    locating.add_argument(
        "--focus",
        action="store_true",
        help="focus the agent pane; for a non-agent service, focus its tab and print the pane",
    )
    locating.set_defaults(json=False, width=None)
    for command in (listing, answering, sending):
        command.add_argument("--json", action="store_true")
        command.add_argument(
            "--width", type=int, default=None, help="plain output width in terminal cells"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    width = display.terminal_width() if args.width is None else args.width
    try:
        if width < 4:
            raise questions.ProviderError("width must be at least 4")
        settings = config.load()
        if args.command == "where":
            return where(
                CliHerdr(), args.name, focus=args.focus, separator=settings.panes.separator
            )
        adapters = settings.questions.adapters
        if args.command == "q" and not adapters:
            message = "No question providers configured."
            if args.json:
                emit({"configured": False, "items": [], "errors": [{"error": message}]})
            else:
                print_text(message, width)
            return cli.EXIT_UNAVAILABLE
        principal = settings.human.name
        if not principal.strip() or len(principal) > 64 or any(ord(c) < 32 for c in principal):
            raise config.ConfigError("configure human.name before using ppw")
        if args.command == "send":
            target = args.to or settings.human.default_to
            if not target:
                raise config.ConfigError("configure human.default_to or pass --to")
            command = ["send", target, args.text, "--source", principal, "--human"]
            for path in args.file:
                command += ["--file", path]
            if args.json:
                command.append("--json")
            try:
                result = cli.main(command)
            except SystemExit as exc:
                result = int(exc.code)
            if result and args.json:
                emit({"error": "send failed; see stderr", "exit_code": result})
            return result
        if args.command == "q" and args.id is None:
            items, errors = [], []
            for name, provider in adapters.items():
                try:
                    for item in questions.listing(provider, principal):
                        if args.all or item["state"] == "open":
                            items.append(item | {"id": f"{name}:{item['id']}"})
                except questions.ProviderError as exc:
                    errors.append({"provider": name, "error": str(exc)})
            if args.json:
                emit({"configured": True, "principal": principal, "items": items, "errors": errors})
            else:
                print_text(f"{len(items)} questions for {principal}", width)
                for item in items:
                    print_text(f"{item['id']}  from {item['from']}  {item['state']}", width)
                    print_text(item["text"].splitlines()[0], width)
                for error in errors:
                    print_text(f"ERROR {error['provider']}: {error['error']}", width)
            return cli.EXIT_FAILED if errors else 0
        name, provider, key = qualified(adapters, args.id)
        if args.command == "q":
            item = questions.get(provider, principal, key) | {"id": f"{name}:{key}"}
            if args.json:
                emit({"item": item})
            else:
                print_text(
                    f"{item['id']}  {item['state']}\nFrom: {item['from']}\nTo: {item['to']}", width
                )
                print_text(item["text"], width)
                for field in ("links", "options"):
                    if field in item:
                        print_text(f"{field}: {json.dumps(item[field], ensure_ascii=False)}", width)
            return 0
        if args.file:
            with args.file.open("rb") as stream:
                text = stream.read(questions.MAX_INPUT + 1).decode("utf-8-sig")
        else:
            text = args.text
        text = text.replace("\r\n", "\n")
        if not text.strip() or len(text) > 20_000:
            raise questions.ProviderError("answer must contain 1..20000 characters")
        receipt = questions.answer(provider, principal, key, text)
        if args.json:
            emit({"id": args.id, **receipt})
        else:
            print_text(
                f"{args.id}: {receipt['state']}\nReceipt: {receipt['receipt']}\n"
                f"Notification: {receipt['notification']}",
                width,
            )
        return 0
    except (config.ConfigError, questions.ProviderError, HerdrError, OSError, UnicodeError) as exc:
        if args.json:
            emit({"error": str(exc)})
        else:
            print_text(f"ppw: {exc}", max(4, width))
        return cli.EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
