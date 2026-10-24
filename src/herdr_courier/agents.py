"""courier agents: every pane as a command destination, with the names a phone screen leaves out.

herdr's mobile layout (terminal width at or below ``ui.mobile_width_threshold``, 64 by default)
draws the workspace label, ``tab <label> · n/m`` and status counts in its header. Pane names, agent
names and pane borders are not drawn there (herdr 0.9.5, ``src/client/shell/mobile.rs``). On a
phone the tab label is the only identity on screen, so a tab has to be named after the agent it
holds; this table joins tabs, panes, agents and courier serve's queue so the mismatches show.
"""

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .display import cols, fit, wrap

NO_AGENT = "shell"


class Workspace(Protocol):
    def agent_list(self) -> list[dict[str, Any]]: ...
    def tab_list(self) -> list[dict[str, Any]]: ...
    def pane_list(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Row:
    pane_id: str
    tab_id: str
    tab: str
    tab_number: int
    agent: str | None
    kind: str | None
    status: str
    focused: bool
    title: str
    queued: int
    hold: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect(herdr: Workspace, dispatch: dict[str, Any] | None) -> list[Row]:
    """One row per pane, in tab order; courier serve's queue and holds are joined in when it answered."""
    tabs = {t["tab_id"]: t for t in herdr.tab_list()}
    agents = {a["pane_id"]: a for a in herdr.agent_list()}
    queues = (dispatch or {}).get("panes") or {}
    rows = []
    for pane in herdr.pane_list():
        pane_id = pane["pane_id"]
        tab = tabs.get(pane.get("tab_id"), {})
        agent = agents.get(pane_id)
        queue = queues.get(pane_id) or {}
        hold = queue.get("hold")
        rows.append(
            Row(
                pane_id=pane_id,
                tab_id=pane.get("tab_id", ""),
                tab=tab.get("label", ""),
                tab_number=int(tab.get("number", 0)),
                agent=agent.get("name") if agent else None,
                kind=agent.get("agent") if agent else None,
                status=agent.get("agent_status", "unknown") if agent else NO_AGENT,
                focused=bool(pane.get("focused")),
                title=pane.get("terminal_title_stripped") or pane.get("terminal_title") or "",
                queued=len(queue.get("pending") or []),
                hold=None if hold is None else (hold.get("reason") or "hold"),
            )
        )
    rows.sort(key=lambda r: (r.tab_number, r.pane_id))
    return rows


def warnings(rows: list[Row]) -> list[str]:
    """Where the phone header would mislead: an agent the tab does not name, or no name at all."""
    out = []
    for row in rows:
        if row.status == NO_AGENT:
            continue
        if row.agent is None:
            out.append(
                f"{row.pane_id} (tab {row.tab!r}) runs {row.kind} without a name: "
                f"herdr agent rename {row.pane_id} <name>"
            )
        elif row.agent != row.tab:
            out.append(
                f"tab {row.tab!r} holds agent {row.agent!r}: rename the tab "
                f"(herdr tab rename {row.tab_id} {row.agent}) so the phone header names the agent"
            )
    return out


def _queue_cell(row: Row) -> str:
    parts = []
    if row.queued:
        parts.append(f"q{row.queued}")
    if row.hold is not None:
        parts.append("hold")
    return " ".join(parts) or "-"


def render(
    rows: list[Row],
    notes: list[str],
    *,
    width: int,
    dispatcher: str,
    wide: bool = False,
) -> str:
    """A table that fits a phone terminal; ``wide`` adds the terminal title column."""
    width = max(width, 40)
    pane_w = max([len("pane"), *(cols(r.pane_id) for r in rows)])
    status_w = max([len("status"), *(cols(r.status) for r in rows)])
    queue_w = max([len("queue"), *(cols(_queue_cell(r)) for r in rows)])
    fixed = 2 + pane_w + 2 + 2 + status_w + 2 + queue_w
    flexible = width - fixed - 2
    tab_w = max(6, flexible // 2)
    agent_w = max(6, flexible - tab_w)
    if wide:
        tab_w = max(6, flexible // 3)
        agent_w = max(6, flexible // 3)

    def line(mark: str, pane: str, tab: str, agent: str, status: str, queue_cell: str, title: str):
        cells = [
            mark,
            fit(pane, pane_w),
            fit(tab, tab_w),
            fit(agent, agent_w),
            fit(status, status_w),
            fit(queue_cell, queue_w),
        ]
        text = " ".join(cells)
        if wide:
            text += " " + fit(title, max(0, width - cols(text) - 1))
        return text.rstrip()

    out = [
        *wrap(f"dispatcher: {dispatcher}", width, "", "  "),
        line(" ", "pane", "tab", "agent", "status", "queue", "title"),
    ]
    for row in rows:
        out.append(
            line(
                "*" if row.focused else " ",
                row.pane_id,
                row.tab,
                row.agent or "-",
                row.status,
                _queue_cell(row),
                row.title,
            )
        )
    out.append(fit("* = focused on the herdr server (any client)", width).rstrip())
    out.append(fit("shell = no agent in that pane", width).rstrip())
    if notes:
        out.append("")
        for note in notes:
            out.extend(wrap(note, width, "! ", "  "))
    return "\n".join(out)
