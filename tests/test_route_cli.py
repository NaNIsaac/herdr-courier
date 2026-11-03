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
def served(start, herdr, monkeypatch, tmp_path):
    """courier serve with boss, boss-deputy, lane and secretary; the CLI asks it from outside herdr."""
    for pane, name in (("w1:p1", "boss"), ("w1:p2", "boss-deputy"), ("w1:p3", "lane")):
        herdr.add(pane, name)
    herdr.add("w1:p4", "secretary")
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    monkeypatch.setattr(cli, "CliHerdr", lambda: FakeHerdr("boss", "boss-deputy", "lane"))
    start(herdr)
    return tmp_path


def test_route_sets_lists_and_clears(served, capsys):
    assert cli.main(["route", "boss", "boss-deputy"]) == 0
    assert capsys.readouterr().out == "boss → boss-deputy\n"
    assert routes.load(served) == {"boss": "boss-deputy"}
    assert cli.main(["route"]) == 0
    assert capsys.readouterr().out == "boss → boss-deputy\n"
    assert cli.main(["route", "boss", "--clear"]) == 0
    assert capsys.readouterr().out == "boss: route cleared\n"
    assert routes.load(served) == {}
    assert cli.main(["route"]) == 0
    assert capsys.readouterr().out == "no routes\n"
    assert cli.main(["route", "boss", "--clear"]) == 0
    assert capsys.readouterr().out == "no route for boss\n"
    log = (served / "routes.log").read_text(encoding="utf-8")
    assert "boss → boss-deputy by a terminal outside herdr" in log


def test_a_lane_pane_is_refused(served, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p3")
    with pytest.raises(SystemExit) as exit_:
        cli.main(["route", "boss", "lane"])
    assert exit_.value.code == cli.EXIT_FAILED
    assert "route changes need boss, secretary" in capsys.readouterr().err
    assert not routes.path(served).exists()


def test_boss_and_secretary_panes_may_change_a_route(served, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    assert cli.main(["route", "boss", "boss-deputy"]) == 0
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p4")
    assert cli.main(["route", "boss", "--clear"]) == 0
    assert capsys.readouterr().out.splitlines() == ["boss → boss-deputy", "boss: route cleared"]


def test_a_loop_is_refused_and_the_route_kept(served, capsys):
    cli.main(["route", "boss", "boss-deputy"])
    with pytest.raises(SystemExit):
        cli.main(["route", "boss-deputy", "boss"])
    assert "route loop: boss-deputy → boss → boss-deputy" in capsys.readouterr().err
    assert routes.load(served) == {"boss": "boss-deputy"}


def test_a_route_to_an_agent_no_pane_runs_is_set_with_a_warning(served, capsys):
    assert cli.main(["route", "boss", "boss-2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["routes"] == {"boss": "boss-2"}
    assert out["warnings"] == [
        "boss → boss-2, but no pane runs boss-2: messages for boss go to boss"
    ]


def test_a_change_needs_courier_serve_but_a_list_does_not(state_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CliHerdr", lambda: FakeHerdr("boss"))
    routes.save(state_dir, {"boss": "boss-deputy"})
    with pytest.raises(SystemExit) as exit_:
        cli.main(["route", "boss", "--clear"])
    assert exit_.value.code == cli.EXIT_UNAVAILABLE
    assert routes.load(state_dir) == {"boss": "boss-deputy"}
    capsys.readouterr()
    assert cli.main(["route"]) == 0
    out = capsys.readouterr()
    assert out.out == "boss → boss-deputy\n"
    assert "courier serve is not running" in out.err


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
