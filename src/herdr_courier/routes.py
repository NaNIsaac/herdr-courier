"""Name routes: messages for one agent name go to another, as when a deputy stands in.

courier serve keeps the routes in ``routes.json`` in the state directory, a JSON object from
name to name, and reads it when it starts. A route changes only through ``courier route``,
which asks courier serve, and each change adds a line to ``routes.log``. Only names are
routed, never pane ids.
"""

import json
import os
import time
import uuid
from pathlib import Path

MAX_HOPS = 8


class RouteError(Exception):
    pass


def path(root: Path) -> Path:
    return root / "routes.json"


def load(root: Path) -> dict[str, str]:
    """The routes in ``root``; no file is no routes. A file that is not a name map raises."""
    try:
        raw = path(root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RouteError(f"{path(root)} is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k and v for k, v in data.items()
    ):
        raise RouteError(f"{path(root)} must map agent names to agent names")
    return data


def save(root: Path, routes: dict[str, str]) -> None:
    """Replace the file in one step, so a crash never leaves half of it for the next start."""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"routes.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(routes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path(root))


def audit(root: Path, line: str) -> None:
    """One line per change, with the local time: who changed which route."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with (root / "routes.log").open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")


def chain(routes: dict[str, str], name: str) -> list[str]:
    """``name`` and every name it is routed on to, in order."""
    out = [name]
    while (nxt := routes.get(out[-1])) is not None:
        if nxt in out or len(out) > MAX_HOPS:
            raise RouteError(f"route loop: {' → '.join([*out, nxt])}")
        out.append(nxt)
    return out


def resolve(routes: dict[str, str], name: str) -> str:
    return name if is_pane_id(name) else chain(routes, name)[-1]


def is_pane_id(target: str) -> bool:
    return target.count(":") == 1 and target.split(":")[1].startswith("p")


def with_route(routes: dict[str, str], name: str, to: str) -> dict[str, str]:
    """``routes`` with ``name`` sent to ``to``; refused when that would make a loop."""
    if name == to:
        raise RouteError(f"{name} cannot be routed to itself")
    for value in (name, to):
        if is_pane_id(value):
            raise RouteError(f"{value} is a pane id; routes take agent names")
    changed = routes | {name: to}
    chain(changed, name)
    return changed


def describe(routes: dict[str, str]) -> list[str]:
    return [f"{name} → {to}" for name, to in sorted(routes.items())]


def unmet(routes: dict[str, str], running: set[str]) -> list[str]:
    """A warning for each routed name whose last target no pane runs, so its messages stay put."""
    out = []
    for name in sorted(routes):
        try:
            to = resolve(routes, name)
        except RouteError as exc:
            out.append(str(exc))
            continue
        if to not in running:
            out.append(f"{name} → {to}, but no pane runs {to}: messages for {name} go to {name}")
    return out
