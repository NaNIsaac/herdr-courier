import json

import pytest

from herdr_courier import cli, display, panes
from herdr_courier.core.transport import HerdrError


class FakeWorkspace:
    def __init__(self):
        self.tabs = [
            {"tab_id": "w1:t2M", "label": "infra", "number": 84},
            {"tab_id": "w1:t8", "label": "boss", "number": 8},
            {"tab_id": "w1:t19", "label": "xrr-policy", "number": 41},
        ]
        self.panes = [
            {"pane_id": "w1:p4Z", "tab_id": "w1:t2M", "label": "infra · courierd", "cwd": "C:\\"},
            {
                "pane_id": "w1:p4W",
                "tab_id": "w1:t2M",
                "label": "infra · sentinel",
                "cwd": "C:\\",
                "focused": True,
            },
            {
                "pane_id": "w1:p1S",
                "tab_id": "w1:t8",
                "label": "core · boss",
                "cwd": "C:\\src\\team",
                "agent": "claude",
            },
            {
                "pane_id": "w1:p3H",
                "tab_id": "w1:t19",
                "label": "lane · xrr-policy",
                "cwd": "C:\\src\\team\\_wt\\analysis-fit-policy-x2d\\notebooks",
                "agent": "claude",
            },
            {
                "pane_id": "w1:p3J",
                "tab_id": "w1:t19",
                "label": "xrr-policy › sweep",
                "cwd": "C:\\src",
            },
        ]

    def tab_list(self):
        return self.tabs

    def pane_list(self):
        return self.panes

    def agent_list(self):
        return []


@pytest.fixture(autouse=True)
def dev(monkeypatch):
    monkeypatch.setenv("DEV", "C:\\src")
    monkeypatch.delenv("DATA", raising=False)


def test_collect_groups_by_tab_in_tab_order():
    tabs = panes.collect(FakeWorkspace())
    assert [t.label for t in tabs] == ["boss", "xrr-policy", "infra"]
    assert [p.pane_id for p in tabs[2].panes] == ["w1:p4W", "w1:p4Z"]
    assert tabs[1].panes[0].agent == "claude"


def test_roster_labels_pass():
    assert panes.warnings(panes.collect(FakeWorkspace())) == []


@pytest.mark.parametrize(
    ("label", "tab", "expect"),
    [
        ("", "hc-lab", 'has no label: herdr pane rename w1:p9 "lane · hc-lab"'),
        ("claude", "hc-lab", "does not start with lane, core, isaac or infra"),
        ("lane - hc-lab", "hc-lab", "does not start with"),
        ("lane · HC_Lab", "hc-lab", "lowercase letters, digits and hyphens"),
        ("lane · boss", "hc-lab", 'herdr pane rename w1:p9 "lane · hc-lab"'),
        ("core · boss2", "boss", "core panes are named after their tab"),
        ("boss › sweep", "hc-lab", "names tab 'boss' but sits in 'hc-lab'"),
        ("hc-lab › ", "hc-lab", "has no task"),
    ],
)
def test_label_problems(label, tab, expect):
    pane = panes.Pane("w1:p9", label, "", None, False)
    problem = panes.label_problem(pane, tab)
    assert problem is not None
    assert expect in problem


@pytest.mark.parametrize("label", ["infra · status-serve", "hc-lab › replay 2", "lane · hc-lab"])
def test_label_ok(label):
    assert panes.label_problem(panes.Pane("w1:p9", label, "", None, False), "hc-lab") is None


def test_short_path(monkeypatch):
    assert panes.short_path("C:\\src\\team") == "$DEV\\team"
    assert panes.short_path("C:\\src") == "$DEV"
    assert panes.short_path("C:\\srcX") == "C:\\srcX"
    monkeypatch.setenv("DATA", "E:\\data\\")
    assert panes.short_path("E:\\data\\runs") == "$DATA\\runs"


def test_render_fits_a_phone_and_keeps_path_tails():
    ws = FakeWorkspace()
    ws.panes.append({"pane_id": "w1:p50", "tab_id": "w1:t8", "label": "", "cwd": "C:\\x"})
    tabs = panes.collect(ws)
    lines = panes.render(tabs, panes.warnings(tabs), width=62).splitlines()
    assert max(display.cols(line) for line in lines) <= 62
    assert lines[0] == "tab boss  w1:t8  2 panes"
    deep = next(line for line in lines if "w1:p3H" in line)
    assert deep.endswith((display.ELLIPSIS + "notebooks", "\\notebooks"))
    assert any(line.startswith("* w1:p4W") for line in lines)
    assert any(line.startswith("! w1:p50 in tab 'boss' has no label") for line in lines)


def test_clip_left():
    assert display.clip_left("abcdef", 6) == "abcdef"
    assert display.clip_left("abcdef", 4) == display.ELLIPSIS + "def"
    assert display.cols(display.clip_left("가나다라", 5)) <= 5


def test_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CliHerdr", FakeWorkspace)
    assert cli.main(["panes", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"] == []
    assert data["tabs"][0]["panes"][0]["label"] == "core · boss"


def test_cli_reports_when_herdr_is_down(monkeypatch, capsys):
    class Down(FakeWorkspace):
        def tab_list(self):
            raise HerdrError("cli_timeout", "herdr tab list took over 15 s")

    monkeypatch.setattr(cli, "CliHerdr", Down)
    assert cli.main(["panes"]) == cli.EXIT_FAILED
    assert "herdr did not answer" in capsys.readouterr().err
