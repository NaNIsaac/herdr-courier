import json
import os
import subprocess
import sys
import urllib.error

import pytest

from herdr_console import cli, display, doctor
from herdr_console.client import Unavailable
from herdr_console.core.transport import HerdrError
from herdr_console.doctor import Level

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
        self.hcd = status_reply()
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

    def hcd_status(self, root):
        if isinstance(self.hcd, Exception):
            raise self.hcd
        return self.hcd

    def pid_alive(self, pid):
        return pid in self.alive

    def ovis_health(self, url):
        if isinstance(self.ovis, Exception):
            raise self.ovis
        return self.ovis


@pytest.fixture
def roots(tmp_path):
    root, icc = tmp_path / "hcd", tmp_path / "icc"
    root.mkdir()
    (root / "endpoint.json").write_text(json.dumps({"port": 65499, "token": "t", "pid": 408}))
    return root, icc


def run(probes, roots):
    root, icc = roots
    return {c.name: c for c in doctor.diagnose(probes, root=root, icc_root=icc, now=NOW)}


def test_healthy(roots):
    checks = run(FakeProbes(), roots)
    assert {c.level for c in checks.values()} == {Level.OK}
    assert list(checks) == ["herdr", "hcd", "worker", "queue", "journal", "icc", "ovis"]
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


def test_hcd_not_running(roots):
    (roots[0] / "endpoint.json").unlink()
    checks = run(FakeProbes(), roots)
    assert checks["hcd"].level is Level.FAIL
    assert "worker" not in checks
    assert checks["hcd"].fix == doctor.START_HCD


def test_stale_endpoint_and_hung_hcd(roots):
    probes = FakeProbes()
    probes.hcd = Unavailable("cannot reach hcd on port 65499")
    probes.alive = set()
    assert "stale endpoint" in run(probes, roots)["hcd"].detail
    probes.alive = {408}
    hung = run(probes, roots)["hcd"]
    assert hung.level is Level.FAIL
    assert hung.detail.startswith("pid 408 is alive but does not answer")


def test_worker_stalled_or_erroring(roots):
    probes = FakeProbes()
    probes.hcd = status_reply(seconds_since_tick=42.0)
    assert run(probes, roots)["worker"].level is Level.FAIL
    probes.hcd = status_reply(errors=2, last_error="12:00:00 HerdrError('cli_timeout')")
    worker = run(probes, roots)["worker"]
    assert worker.level is Level.WARN
    assert "2 failed passes" in worker.detail


def test_queue_problems(roots):
    probes = FakeProbes()
    probes.hcd["status"] = {
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


def test_second_writer(roots):
    _, icc = roots
    (icc / "dispatch").mkdir(parents=True)
    (icc / "dispatch" / "endpoint.json").write_text(json.dumps({"port": 8765, "pid": 7000}))
    probes = FakeProbes()
    assert "stale endpoint" in run(probes, roots)["icc"].detail
    probes.alive.add(7000)
    assert run(probes, roots)["icc"].level is Level.FAIL


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
    text = doctor.render(doctor.diagnose(probes, root=roots[0], icc_root=roots[1], now=NOW), 62)
    lines = text.splitlines()
    assert lines[0] == "hc doctor: 1 fail, 1 warn"
    assert max(display.cols(line) for line in lines) <= 62
    assert any(line.lstrip().startswith("fix: start hcd") for line in lines)


def test_cli_exit_code(monkeypatch, roots, capsys):
    probes = FakeProbes()
    real = doctor.diagnose
    monkeypatch.setattr(doctor, "LiveProbes", lambda: probes)
    monkeypatch.setattr(
        doctor, "diagnose", lambda p: real(p, root=roots[0], icc_root=roots[1], now=NOW)
    )
    assert cli.main(["doctor"]) == 0
    assert capsys.readouterr().out.startswith("hc doctor: all checks pass")
    probes.panes = HerdrError("cli_timeout", "slow")
    assert cli.main(["doctor", "--json"]) == cli.EXIT_FAILED
    assert json.loads(capsys.readouterr().out)[0]["level"] == "FAIL"


def test_pid_alive():
    assert doctor.pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not doctor.pid_alive(child.pid)
    assert not doctor.pid_alive(0)
