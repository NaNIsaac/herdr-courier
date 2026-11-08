import pytest

from herdr_courier import cli
from herdr_courier.core.transport import HerdrError


class FakeHerdr:
    """critic started the shared Codex app-server; reports is a second Codex pane."""

    calls = 0

    def agent_list(self):
        FakeHerdr.calls += 1
        return [
            {"name": "critic", "pane_id": "w4:pE", "agent_session": {"value": "t-critic"}},
            {"name": "reports", "pane_id": "w4:p15", "agent_session": {"value": "t-reports"}},
            {"name": "shell", "pane_id": "w4:p1A"},
        ]


class DownHerdr:
    def agent_list(self):
        raise HerdrError("server_not_running", "no herdr server is running at the socket")


@pytest.fixture
def codex(monkeypatch):
    for name in ("COURIER_SOURCE", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERDR_PANE_ID", "w4:pE")
    FakeHerdr.calls = 0
    monkeypatch.setattr(cli, "CliHerdr", FakeHerdr)
    return monkeypatch


def test_a_codex_command_is_from_the_pane_of_its_session(codex):
    codex.setenv("CODEX_THREAD_ID", "t-reports")
    assert cli.own_pane() == "w4:p15"
    assert cli.default_source() == "w4:p15"
    assert cli.own_name() == "reports"


def test_without_a_codex_session_herdr_pane_id_stands_and_herdr_is_not_asked(codex):
    assert cli.own_pane() == "w4:pE"
    assert cli.default_source() == "w4:pE"
    assert FakeHerdr.calls == 0


def test_an_unknown_session_or_herdr_down_keeps_herdr_pane_id(codex):
    codex.setenv("CODEX_THREAD_ID", "t-gone")
    assert cli.own_pane() == "w4:pE"
    codex.setattr(cli, "CliHerdr", DownHerdr)
    codex.setenv("CODEX_THREAD_ID", "t-reports")
    assert cli.own_pane() == "w4:pE"


def test_courier_source_still_wins(codex):
    codex.setenv("CODEX_THREAD_ID", "t-reports")
    codex.setenv("COURIER_SOURCE", "bench")
    assert cli.default_source() == "bench"


def test_building_the_parser_does_not_ask_herdr(codex):
    codex.setenv("CODEX_THREAD_ID", "t-reports")
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert FakeHerdr.calls == 0
