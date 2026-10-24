"""courier panes: the workspace laid out by tab, with each pane's label checked against a rule.

Pane labels show in Ovis rows and desktop borders and are cut from the right on a phone, so the
default rule (``defaults``, overridden by ``[panes]`` in config.toml) is ``<prefix> · <name>``:
panes under a ``named_after_tab`` prefix carry their tab's label as the name, ``named_freely``
prefixes any name, and a worker another pane starts is ``<tab> › <task>``. ``courier agents``
covers agent names against tab labels; this covers the pane labels and where each pane is working.
"""

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import display
from .agents import Workspace
from .config import Panes

DEFAULT_RULE = Panes()
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True)
class Pane:
    pane_id: str
    label: str
    cwd: str
    agent: str | None
    focused: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Tab:
    tab_id: str
    label: str
    number: int
    panes: list[Pane]

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "panes": [p.as_dict() for p in self.panes]}


def collect(herdr: Workspace) -> list[Tab]:
    tabs = {t["tab_id"]: t for t in herdr.tab_list()}
    grouped: dict[str, list[Pane]] = {tab_id: [] for tab_id in tabs}
    for pane in herdr.pane_list():
        grouped.setdefault(pane.get("tab_id", ""), []).append(
            Pane(
                pane_id=pane["pane_id"],
                label=pane.get("label") or "",
                cwd=pane.get("cwd") or "",
                agent=pane.get("agent"),
                focused=bool(pane.get("focused")),
            )
        )
    out = [
        Tab(
            tab_id=tab_id,
            label=tabs.get(tab_id, {}).get("label", ""),
            number=int(tabs.get(tab_id, {}).get("number", 0)),
            panes=sorted(panes, key=lambda p: p.pane_id),
        )
        for tab_id, panes in grouped.items()
    ]
    return sorted(out, key=lambda t: (t.number, t.tab_id))


def label_problem(pane: Pane, tab: str, rule: Panes = DEFAULT_RULE) -> str | None:
    """Why ``pane``'s label breaks the naming rule, or None when it follows it.

    A rule with no prefixes checks nothing.
    """
    prefixes = (*rule.named_after_tab, *rule.named_freely)
    if not prefixes:
        return None
    dot, sub = rule.separator, rule.worker_separator
    suggestion = f'herdr pane rename {pane.pane_id} "{prefixes[0]}{dot}{tab}"'
    if not pane.label:
        return f"{pane.pane_id} in tab {tab!r} has no label: {suggestion}"
    if sub in pane.label:
        owner, _, task = pane.label.partition(sub)
        if owner != tab:
            return f"{pane.pane_id} {pane.label!r} names tab {owner!r} but sits in {tab!r}"
        if not task.strip():
            return f"{pane.pane_id} {pane.label!r} has no task after the {sub.strip()}"
        return None
    prefix, found, name = pane.label.partition(dot)
    if not found or prefix not in prefixes:
        allowed = (
            ", ".join(prefixes[:-1]) + " or " + prefixes[-1] if len(prefixes) > 1 else prefixes[0]
        )
        return (
            f"{pane.pane_id} {pane.label!r} does not start with {allowed} "
            f"and {dot.strip()}: {suggestion}"
        )
    if not NAME.fullmatch(name):
        return f"{pane.pane_id} {pane.label!r}: names are lowercase letters, digits and hyphens"
    if prefix in rule.named_after_tab and name != tab:
        return (
            f"{pane.pane_id} {pane.label!r} is in tab {tab!r}; {prefix} panes are named after their "
            f'tab: herdr pane rename {pane.pane_id} "{prefix}{dot}{tab}"'
        )
    return None


def warnings(tabs: list[Tab], rule: Panes = DEFAULT_RULE) -> list[str]:
    return [p for t in tabs for pane in t.panes if (p := label_problem(pane, t.label, rule))]


def short_path(path: str, variables: tuple[str, ...] = DEFAULT_RULE.path_variables) -> str:
    """Spell a working directory through the first variable it falls under, else through ~."""
    for var in variables:
        root = os.environ.get(var, "").rstrip("/\\")
        if root and _under(path, root):
            return f"${var}{path[len(root) :]}"
    home = str(Path.home()).rstrip("/\\")
    if _under(path, home):
        return "~" + path[len(home) :]
    return path


def _under(path: str, root: str) -> bool:
    # herdr reports Windows paths even when this runs elsewhere (tests), so both separators count.
    fold = str.casefold if os.name == "nt" else str
    p, r = fold(path), fold(root)
    return p == r or (p.startswith(r) and p[len(r)] in "/\\")


def render(tabs: list[Tab], notes: list[str], *, width: int, rule: Panes = DEFAULT_RULE) -> str:
    width = max(width, 40)
    pane_w = max([6, *(display.cols(p.pane_id) for t in tabs for p in t.panes)])
    label_w = min(
        max([5, *(display.cols(p.label) for t in tabs for p in t.panes)]),
        (width - pane_w - 4) * 3 // 5,
    )
    cwd_w = width - (2 + pane_w + 1 + label_w + 1)
    out = []
    for tab in tabs:
        count = f"  {len(tab.panes)} panes" if len(tab.panes) > 1 else ""
        out.append(display.fit(f"tab {tab.label or '-'}  {tab.tab_id}{count}", width).rstrip())
        for pane in tab.panes:
            line = " ".join(
                [
                    "*" if pane.focused else " ",
                    display.fit(pane.pane_id, pane_w),
                    display.fit(pane.label or "-", label_w),
                    display.clip_left(short_path(pane.cwd, rule.path_variables), cwd_w),
                ]
            )
            out.append(line.rstrip())
    out.append(display.fit("* = focused on the herdr server (any client)", width).rstrip())
    if notes:
        out.append("")
        for note in notes:
            out.extend(display.wrap(note, width, "! ", "  "))
    return "\n".join(out)
