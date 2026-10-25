import json
import os
import subprocess
import sys
import urllib.error

import pytest

from herdr_courier import cli, display, doctor
from herdr_courier.client import Unavailable
from herdr_courier.config import Config, Doctor
from herdr_courier.core.transport import HerdrError
from herdr_courier.doctor import Level

NOW = 1_800_000_000.0


def status_reply(**worker):
    return {
        "api": 1,
        "worker": {"alive": True, "seconds_since_tick": 0.3, "errors": 0, **worker},
        "status": {"panes": {}, "recent": []},
    }


class FakeProbes:
    def __init__(self):
        self.version = "0.9.5"
        self.panes = [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2"}]
        self.agents = [{"pane_id": "w1:p1", "agent_status": "idle"}]
        self.daemon = status_reply()
        self.alive = {408}
        self.ovis = {
            "ok": True,
            "herdr": {"version": "0.9.5"},
            "web_ui": {"revision": "d5a35b7a44"},
        }

    def herdr_version(self):
        if isinstance(self.version, Exception):
            raise self.version
        return self.version

    def pane_list(self):
        if isinstance(self.panes, Exception):
            raise self.panes
        return self.panes

    def agent_list(self):
        return self.agents

    def daemon_status(self, root):
        if isinstance(self.daemon, Exception):
            raise self.daemon
        return self.daemon

    def pid_alive(self, pid):
        return pid in self.alive

    def ovis_health(self, url):
        if isinstance(self.ovis, Exception):
            raise self.ovis
        return self.ovis


@pytest.fixture
def roots(tmp_path):
    root, other = tmp_path / "state", tmp_path / "other"
    root.mkdir()
    (root / "endpoint.json").write_text(json.dumps({"port": 65499, "token": "t", "pid": 408}))
    return root, other


def settings(other):
    return Config(doctor=Doctor(other_writers=(str(other / "endpoint.json"),)))


def run(probes, roots, config=None):
    root, other = roots
    config = config or settings(other)
    return {c.name: c for c in doctor.diagnose(probes, root=root, config=config, now=NOW)}


def test_healthy(roots):
    checks = run(FakeProbes(), roots)
    assert {c.level for c in checks.values()} == {Level.OK}
    assert list(checks) == [
        "herdr",
        "dispatcher",
        "worker",
        "queue",
        "journal",
        "config",
        "writers",
        "ovis",
    ]
    assert checks["herdr"].detail == "0.9.5; 2 panes, 1 agents"


def test_herdr_missing_or_not_answering(roots):
    probes = FakeProbes()
    probes.panes = HerdrError("cli_timeout", "herdr pane list took over 15 s")
    checks = run(probes, roots)
    assert checks["herdr"].level is Level.FAIL
    assert "does not answer" in checks["herdr"].detail
    probes.version = HerdrError("cli_missing", "not found")
    checks = run(probes, roots)
    assert checks["herdr"].level is Level.FAIL
    assert checks["ovis"].level is Level.OK


def test_blocked_agent_is_a_warning(roots):
    probes = FakeProbes()
    probes.agents.append({"pane_id": "w1:p2", "agent_status": "blocked"})
    assert run(probes, roots)["herdr"].level is Level.WARN


def test_dispatcher_not_running(roots):
    (roots[0] / "endpoint.json").unlink()
    checks = run(FakeProbes(), roots)
    assert checks["dispatcher"].level is Level.FAIL
    assert "worker" not in checks
    assert checks["dispatcher"].fix == doctor.START_DAEMON


def test_stale_endpoint_and_hung_dispatcher(roots):
    probes = FakeProbes()
    probes.daemon = Unavailable("cannot reach courier serve on port 65499")
    probes.alive = set()
    assert "stale endpoint" in run(probes, roots)["dispatcher"].detail
    probes.alive = {408}
    hung = run(probes, roots)["dispatcher"]
    assert hung.level is Level.FAIL
    assert hung.detail.startswith("pid 408 is alive but does not answer")


def test_worker_stalled_or_erroring(roots):
    probes = FakeProbes()
    probes.daemon = status_reply(seconds_since_tick=42.0)
    assert run(probes, roots)["worker"].level is Level.FAIL
    probes.daemon = status_reply(errors=2, last_error="12:00:00 HerdrError('cli_timeout')")
    worker = run(probes, roots)["worker"]
    assert worker.level is Level.WARN
    assert "2 failed passes" in worker.detail


def test_queue_problems(roots):
    probes = FakeProbes()
    probes.daemon["status"] = {
        "panes": {
            "w1:p1": {
                "hold": {"seconds_left": None},
                "pending": [{"id": "a1", "created": NOW - 900, "reason": "input line not empty"}],
            },
            "w1:p2": {"hold": {"seconds_left": 30}, "pending": []},
        },
        "recent": [
            {"state": "failed", "updated": NOW - 7200, "reason": "old"},
            {"state": "failed", "updated": NOW - 60, "reason": "pane gone"},
            {"state": "sent", "updated": NOW - 30, "reason": "agent working after submit"},
        ],
    }
    queue = run(probes, roots)["queue"]
    assert queue.level is Level.WARN
    assert queue.detail.startswith("1 queued, 2 held; 1 waiting over 10 min (oldest a1")
    assert "held with no expiry: w1:p1" in queue.detail
    assert "1 failed in the last hour (last: pane gone)" in queue.detail


def test_journal_with_unreadable_lines(roots):
    (roots[0] / "journal.jsonl").write_text('{"event": "hold"}\nnot json\n', encoding="utf-8")
    journal = run(FakeProbes(), roots)["journal"]
    assert journal.level is Level.WARN
    assert "2 entries" in journal.detail
    assert "1 unreadable" in journal.detail


def test_other_writer(roots):
    _, other = roots
    other.mkdir()
    (other / "endpoint.json").write_text(json.dumps({"port": 8765, "pid": 7000}))
    probes = FakeProbes()
    assert "stale endpoint" in run(probes, roots)["writers"].detail
    probes.alive.add(7000)
    writers = run(probes, roots)["writers"]
    assert writers.level is Level.FAIL
    assert "pid 7000" in writers.detail


def test_config_file_is_read_and_checked(roots):
    root, _ = roots
    checks = {c.name: c for c in doctor.diagnose(FakeProbes(), root=root, now=NOW)}
    assert checks["config"].detail.startswith("built-in defaults")
    (root / "config.toml").write_text('[doctor]\nother_writers = []\novis_url = ""\n')
    checks = {c.name: c for c in doctor.diagnose(FakeProbes(), root=root, now=NOW)}
    assert checks["config"].detail == "config.toml in the state directory"
    assert checks["writers"].detail == "no other writers configured"
    assert "ovis" not in checks
    (root / "config.toml").write_text("[doctor]\novis = 1\n")
    checks = {c.name: c for c in doctor.diagnose(FakeProbes(), root=root, now=NOW)}
    assert checks["config"].level is Level.FAIL
    assert "unknown key doctor.ovis" in checks["config"].detail
    assert checks["ovis"].level is Level.OK


def test_ovis(roots):
    probes = FakeProbes()
    probes.ovis = urllib.error.URLError(ConnectionRefusedError("refused"))
    assert run(probes, roots)["ovis"].level is Level.WARN
    probes.ovis = {"ok": True, "herdr": {"version": "0.9.4"}, "web_ui": {}}
    ovis = run(probes, roots)["ovis"]
    assert ovis.level is Level.WARN
    assert ovis.detail.endswith("CLI is 0.9.5")


def test_render_fits_a_phone(roots):
    (roots[0] / "endpoint.json").unlink()
    probes = FakeProbes()
    probes.ovis = urllib.error.URLError(ConnectionRefusedError("refused " * 10))
    text = doctor.render(
        doctor.diagnose(probes, root=roots[0], config=settings(roots[1]), now=NOW), 62
    )
    lines = text.splitlines()
    assert lines[0] == "courier doctor: 1 fail, 1 warn"
    assert max(display.cols(line) for line in lines) <= 62
    assert any(line.lstrip().startswith("fix: start courier serve") for line in lines)


def test_cli_exit_code(monkeypatch, roots, capsys):
    probes = FakeProbes()
    real = doctor.diagnose
    monkeypatch.setattr(doctor, "LiveProbes", lambda: probes)
    monkeypatch.setattr(
        doctor, "diagnose", lambda p: real(p, root=roots[0], config=settings(roots[1]), now=NOW)
    )
    assert cli.main(["doctor"]) == 0
    assert capsys.readouterr().out.startswith("courier doctor: all checks pass")
    probes.panes = HerdrError("cli_timeout", "slow")
    assert cli.main(["doctor", "--json"]) == cli.EXIT_FAILED
    assert json.loads(capsys.readouterr().out)[0]["level"] == "FAIL"


def test_pid_alive():
    assert doctor.pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not doctor.pid_alive(child.pid)
    assert not doctor.pid_alive(0)


def test_dispatcher_names_its_herdr_server_and_fails_from_another(roots, monkeypatch):
    probes = FakeProbes()
    probes.daemon = status_reply() | {"server": os.path.join("herdr", "herdr.sock")}
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    ok = run(probes, roots)["dispatcher"]
    assert ok.level is Level.OK
    assert ok.detail.endswith(f"herdr {os.path.join('herdr', 'herdr.sock')}")
    monkeypatch.setenv("HERDR_SOCKET_PATH", os.path.join("herdr", "sessions", "lab", "herdr.sock"))
    other = run(probes, roots)["dispatcher"]
    assert other.level is Level.FAIL
    assert "this pane is on" in other.detail
