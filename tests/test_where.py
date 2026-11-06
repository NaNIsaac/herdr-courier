import pytest

from herdr_courier import human_cli as H
from herdr_courier.core import transport


class FakeHerdr:
    def __init__(self):
        self.focused = []

    def tab_list(self):
        return [{"tab_id": "w1:t1", "label": "review"}, {"tab_id": "w1:t2", "label": "logs"}]

    def agent_list(self):
        return [{"pane_id": "w1:p1", "name": "reviewer"}]

    def pane_list(self):
        return [
            {"pane_id": "w1:p1", "tab_id": "w1:t1", "label": "agent · review"},
            {"pane_id": "w1:p2", "tab_id": "w1:t2", "label": "service · monitor"},
        ]

    def agent_focus(self, pane):
        self.focused.append(("agent", pane))

    def tab_focus(self, tab):
        self.focused.append(("tab", tab))


@pytest.fixture
def herdr(monkeypatch):
    fake = FakeHerdr()
    monkeypatch.setattr(H, "CliHerdr", lambda: fake)
    return fake


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("reviewer", "review, agent · review, w1:p1"),
        ("agent · review", "review, agent · review, w1:p1"),
        ("monitor", "logs, service · monitor, w1:p2"),
        ("service · monitor", "logs, service · monitor, w1:p2"),
        ("w1:p2", "logs, service · monitor, w1:p2"),
    ],
)
def test_where_without_human_config(herdr, capsys, name, expected):
    assert H.main(["where", name]) == 0
    assert capsys.readouterr().out == expected + "\n"
    assert herdr.focused == []


@pytest.mark.parametrize(
    ("name", "expected"), [("reviewer", ("agent", "w1:p1")), ("monitor", ("tab", "w1:t2"))]
)
def test_focus_uses_agent_or_service_tab(herdr, capsys, name, expected):
    assert H.main(["where", name, "--focus"]) == 0
    assert herdr.focused == [expected]
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_unknown_and_partial_names_never_focus(herdr, capsys):
    for name in ("missing", "view", ""):
        assert H.main(["where", name, "--focus"]) == 1
        assert "no pane matches" in capsys.readouterr().out
    assert herdr.focused == []


def test_duplicate_names_never_focus(herdr, monkeypatch, capsys):
    panes = herdr.pane_list()
    panes[1]["label"] = "service · reviewer"
    monkeypatch.setattr(herdr, "pane_list", lambda: panes)
    assert H.main(["where", "reviewer", "--focus"]) == 1
    assert "ambiguous name" in capsys.readouterr().out
    assert herdr.focused == []


def test_where_reports_transport_failure(herdr, monkeypatch, capsys):
    def fail():
        raise transport.HerdrError("cli_missing", "unavailable")

    monkeypatch.setattr(herdr, "tab_list", fail)
    assert H.main(["where", "monitor", "--focus"]) == 1
    assert "cli_missing" in capsys.readouterr().out
    assert herdr.focused == []


def test_focus_commands_use_resolved_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(transport, "_run", lambda args, timeout: calls.append(args))
    herdr = transport.CliHerdr()
    herdr.agent_focus("w1:p1")
    herdr.tab_focus("w1:t2")
    assert calls == [["agent", "focus", "w1:p1"], ["tab", "focus", "w1:t2"]]


def test_custom_separator_and_control_characters(herdr, monkeypatch, capsys):
    panes = [{"pane_id": "w1:p2", "tab_id": "w1:t2", "label": "service / watch\n\x1b[31m"}]
    monkeypatch.setattr(herdr, "pane_list", lambda: panes)
    assert H.where(herdr, "watch\n\x1b[31m", focus=False, separator=" / ") == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1
    assert "\x1b" not in output
    assert herdr.focused == []


def test_failed_focus_does_not_report_success(herdr, monkeypatch, capsys):
    def fail(_):
        raise transport.HerdrError("target_gone", "pane closed")

    monkeypatch.setattr(herdr, "tab_focus", fail)
    assert H.main(["where", "monitor", "--focus"]) == 1
    assert "target_gone" in capsys.readouterr().out
    assert herdr.focused == []
