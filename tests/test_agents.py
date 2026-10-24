import json

from herdr_courier import agents, cli, display


class FakeWorkspace:
    def __init__(self):
        self.tabs = [
            {"tab_id": "w1:t8", "label": "boss", "number": 8},
            {"tab_id": "w1:t24", "label": "비서", "number": 68},
            {"tab_id": "w1:t27", "label": "arch", "number": 71},
        ]
        self.panes = [
            {
                "pane_id": "w1:p4E",
                "tab_id": "w1:t24",
                "focused": True,
                "terminal_title_stripped": "claude",
            },
            {
                "pane_id": "w1:p1S",
                "tab_id": "w1:t8",
                "focused": False,
                "terminal_title_stripped": "herdr ops",
            },
            {
                "pane_id": "w1:p4H",
                "tab_id": "w1:t27",
                "focused": False,
                "terminal_title": "pwsh.exe",
            },
        ]
        self.agents = [
            {"pane_id": "w1:p1S", "name": "boss", "agent": "claude", "agent_status": "working"},
            {"pane_id": "w1:p4E", "name": "secretary", "agent": "claude", "agent_status": "idle"},
        ]

    def agent_list(self):
        return self.agents

    def tab_list(self):
        return self.tabs

    def pane_list(self):
        return self.panes


DISPATCH = {
    "panes": {
        "w1:p4E": {
            "pending": [{"id": "a"}, {"id": "b"}],
            "hold": {"reason": "typing on the phone"},
        },
    },
    "recent": [],
}


def test_rows_follow_tab_order_and_join_queue_and_holds():
    rows = agents.collect(FakeWorkspace(), DISPATCH)
    assert [r.pane_id for r in rows] == ["w1:p1S", "w1:p4E", "w1:p4H"]
    boss, secretary, shell = rows
    assert (boss.tab, boss.agent, boss.status, boss.queued, boss.hold) == (
        "boss",
        "boss",
        "working",
        0,
        None,
    )
    assert (secretary.focused, secretary.queued, secretary.hold) == (True, 2, "typing on the phone")
    assert (shell.agent, shell.kind, shell.status, shell.title) == (None, None, "shell", "pwsh.exe")


def test_without_daemon_rows_still_come_back():
    rows = agents.collect(FakeWorkspace(), None)
    assert all(r.queued == 0 and r.hold is None for r in rows)


def test_warnings_name_the_tab_that_hides_its_agent():
    ws = FakeWorkspace()
    ws.agents.append({"pane_id": "w1:p4H", "agent": "codex", "agent_status": "idle"})
    notes = agents.warnings(agents.collect(ws, None))
    assert len(notes) == 2
    assert "tab '비서' holds agent 'secretary'" in notes[0]
    assert "herdr tab rename w1:t24 secretary" in notes[0]
    assert "w1:p4H (tab 'arch') runs codex without a name" in notes[1]


def test_render_fits_a_phone_width():
    rows = agents.collect(FakeWorkspace(), DISPATCH)
    text = agents.render(
        rows, agents.warnings(rows), width=62, courierd="running, 2 queued, 1 held"
    )
    lines = text.splitlines()
    assert lines[0] == "courierd: running, 2 queued, 1 held"
    table = lines[1:5]
    assert all(display.cols(line) <= 62 for line in lines)
    assert table[2].startswith("* w1:p4E")
    assert "q2 hold" in table[2]
    assert table[3].split()[-2:] == ["shell", "-"]
    assert any(line.startswith("! tab") for line in lines)


def test_render_truncates_long_labels_instead_of_overflowing():
    ws = FakeWorkspace()
    ws.tabs[0]["label"] = "a tab label that is far too long for a phone"
    rows = agents.collect(ws, None)
    text = agents.render(rows, [], width=40, courierd="not running")
    assert all(len(line) <= 40 for line in text.splitlines()[1:4])
    assert "…" in text


def test_wide_adds_the_title_column():
    rows = agents.collect(FakeWorkspace(), None)
    text = agents.render(rows, [], width=100, courierd="not running", wide=True)
    assert "herdr ops" in text
    assert all(len(line) <= 100 for line in text.splitlines())


def test_cli_agents_json_without_daemon(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CliHerdr", FakeWorkspace)
    assert cli.main(["agents", "--json"]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["courierd"].startswith("not running")
    assert [r["pane_id"] for r in reply["rows"]] == ["w1:p1S", "w1:p4E", "w1:p4H"]
    assert reply["warnings"] == [
        "tab '비서' holds agent 'secretary': rename the tab (herdr tab rename w1:t24 secretary) so the phone header names the agent"
    ]


def test_cli_agents_reports_when_herdr_is_down(monkeypatch, tmp_path, capsys):
    from herdr_courier.core.transport import HerdrError

    class Down:
        def agent_list(self):
            raise HerdrError("cli_missing", "no herdr")

        tab_list = pane_list = agent_list

    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CliHerdr", Down)
    assert cli.main(["agents"]) == cli.EXIT_FAILED
    assert "herdr did not answer" in capsys.readouterr().err


def test_korean_labels_keep_columns_aligned():
    ws = FakeWorkspace()
    ws.tabs[0]["label"] = "boss 조율 + 현황판"
    rows = agents.collect(ws, None)
    lines = agents.render(rows, [], width=62, courierd="not running").splitlines()[1:5]
    status_col = lines[0].index("status")
    assert all(display.cols(line[: line.index(" " * 1, status_col - 1)]) <= 62 for line in lines)
    assert all(display.cols(line) <= 62 for line in lines)
    starts = [
        display.cols(line[: line.index(r.status)]) for line, r in zip(lines[1:], rows, strict=True)
    ]
    assert len(set(starts)) == 1


def test_fit_truncates_wide_characters_by_display_width():
    assert display.cols("비서") == 4
    assert display.fit("비서", 6) == "비서  "
    assert display.fit("비서실장", 5) == "비서…"
    assert display.cols(display.fit("비서실장", 5)) == 5
