import json

import pytest

from herdr_courier import agents, cli, routes
from herdr_courier.core.queue import ROUTE_MISSED


class FakeHerdr:
    def __init__(self, *names):
        self.names = names

    def agent_list(self):
        return [
            {"name": n, "pane_id": f"w1:p{i}", "agent_session": {"value": "s"}}
            for i, n in enumerate(self.names)
        ]


@pytest.fixture
def herdr_with(monkeypatch):
    def use(*names):
        monkeypatch.setattr(cli, "CliHerdr", lambda: FakeHerdr(*names))

    return use


def test_route_sets_lists_and_clears(state_dir, herdr_with, capsys):
    herdr_with("boss", "boss-deputy")
    assert cli.main(["route", "boss", "boss-deputy"]) == 0
    assert capsys.readouterr().out == "boss → boss-deputy\n"
    assert routes.load(state_dir) == {"boss": "boss-deputy"}
    assert cli.main(["route"]) == 0
    assert capsys.readouterr().out == "boss → boss-deputy\n"
    assert cli.main(["route", "boss", "--clear"]) == 0
    assert capsys.readouterr().out == "boss: route cleared\n"
    assert routes.load(state_dir) == {}
    assert cli.main(["route"]) == 0
    assert capsys.readouterr().out == "no routes\n"
    assert cli.main(["route", "boss", "--clear"]) == 0
    assert capsys.readouterr().out == "no route for boss\n"


def test_a_loop_is_refused_and_the_file_kept(state_dir, herdr_with, capsys):
    herdr_with("boss", "boss-deputy")
    cli.main(["route", "boss", "boss-deputy"])
    assert cli.main(["route", "boss-deputy", "boss"]) == cli.EXIT_FAILED
    assert "route loop: boss-deputy → boss → boss-deputy" in capsys.readouterr().err
    assert routes.load(state_dir) == {"boss": "boss-deputy"}


def test_a_route_to_an_agent_no_pane_runs_is_set_with_a_warning(state_dir, herdr_with, capsys):
    herdr_with("boss")
    assert cli.main(["route", "boss", "boss-deputy", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["routes"] == {"boss": "boss-deputy"}
    assert out["warnings"] == [
        "boss → boss-deputy, but no pane runs boss-deputy: messages for boss go to boss"
    ]


def test_the_file_is_replaced_whole(state_dir, herdr_with):
    herdr_with("boss", "boss-deputy")
    cli.main(["route", "boss", "boss-deputy"])
    assert json.loads(routes.path(state_dir).read_text(encoding="utf-8")) == {"boss": "boss-deputy"}
    assert list(state_dir.glob("routes.*.tmp")) == []


def test_status_shows_the_routes():
    reply = {
        "status": {"panes": {}, "held": {}, "recent": [], "routes": {"boss": "boss-deputy"}},
        "worker": {"alive": True, "seconds_since_tick": 0},
    }
    lines = cli.render_status(reply, width=62, recent=10, now=0)
    assert lines[0] == "route boss → boss-deputy"


def test_agents_shows_the_routes_under_the_dispatcher_line():
    text = agents.render([], [], width=62, dispatcher="running", routes=["boss → boss-deputy"])
    assert text.splitlines()[:2] == ["dispatcher: running", "route: boss → boss-deputy"]


def sent(monkeypatch, item):
    monkeypatch.setattr(cli, "request", lambda *a, **k: {"item": item, "server": None})


def test_send_names_the_route_it_took(monkeypatch, capsys):
    item = {
        "id": "abc",
        "target": "boss-deputy",
        "pane_id": "w1:p2",
        "reason": "",
        "routed_from": "boss",
        "state": "queued",
    }
    sent(monkeypatch, item)
    assert cli.main(["send", "boss", "report"]) == 0
    assert capsys.readouterr().out == "queued abc for boss-deputy (w1:p2), routed from boss\n"


def test_send_warns_when_the_route_target_is_not_running(monkeypatch, capsys):
    reason = f"{ROUTE_MISSED}boss → boss-deputy, but boss-deputy is not running"
    item = {
        "id": "abc",
        "target": "boss",
        "pane_id": "w1:p1",
        "reason": reason,
        "routed_from": None,
        "state": "queued",
    }
    sent(monkeypatch, item)
    assert cli.main(["send", "boss", "report"]) == 0
    out = capsys.readouterr()
    assert out.out == "queued abc for boss (w1:p1)\n"
    assert out.err == f"courier: warning: {reason}\n"
