import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_courier import cli, probes, verifier
from herdr_courier.client import Refused, Unavailable, request


@pytest.fixture
def service(herdr, start):
    for name in ("boss", "w1", "w2"):
        herdr.add(name, name)
    _, running = start(herdr)
    return running


def add(service, tmp_path, number, *, present=True):
    key = f"BL-{number}"
    if present:
        (tmp_path / f"out-{number}.txt").write_text("objective output")
    service.dispatcher.send_task(
        "w1",
        "task",
        source="boss",
        task={
            "key": key,
            "evidence": [f"file out-{number}.txt"],
            "cwd": str(tmp_path),
        },
    )
    assert cli.main(["send", "boss", f"DONE {key}", "--source", "w1"]) == 0
    return key


@pytest.mark.parametrize("field,value", [("attempts", 1), ("to", "w2"), ("state", "BLOCKED")])
def test_task_change_during_unlocked_probe_defers_close(
    service, tmp_path, monkeypatch, field, value
):
    key = add(service, tmp_path, 1)
    entered, release = threading.Event(), threading.Event()
    real = probes.check

    def blocked(task, **kwargs):
        entered.set()
        assert release.wait(5)
        return real(task, **kwargs)

    monkeypatch.setattr(probes, "check", blocked)
    d = service.dispatcher
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request, "POST", "await/close", {"key": key, "by": "probe"})
        try:
            assert entered.wait(3)
            assert d.lock.acquire(timeout=0.5), "probe held the dispatcher lock"
            try:
                d.tasks[key][field] = value
                d._task_record(d.tasks[key])
            finally:
                d.lock.release()
            assert cli.main(["send", "w2", "unrelated message"]) == 0
        finally:
            release.set()
        with pytest.raises(Refused, match="task changed during probe; defer"):
            future.result(timeout=5)
    assert d.tasks[key][field] == value
    assert d.tasks[key]["state"] != "DONE"
    assert "probe_results" not in d.tasks[key]


def test_budget_counts_observation_and_service_recheck(service, tmp_path, monkeypatch):
    keys = [add(service, tmp_path, n) for n in (1, 2, 3)]
    calls = []
    real = probes.check

    def counted(task, **kwargs):
        calls.append(task["key"])
        return real(task, **kwargs)

    monkeypatch.setattr(probes, "check", counted)
    outcomes = verifier.run(max_probes=4)
    assert len(calls) == 4
    assert sum(outcome["probe_count"] for outcome in outcomes) == 4
    assert [service.dispatcher.tasks[key]["state"] for key in keys] == ["DONE", "DONE", "VERIFYING"]
    verifier.run(max_probes=4, after=outcomes[-1]["key"])
    assert service.dispatcher.tasks[keys[-1]]["state"] == "DONE"


def test_next_cycle_does_not_starve_behind_failing_tasks(service, tmp_path):
    for n in range(1, 5):
        add(service, tmp_path, n, present=False)
    key = add(service, tmp_path, 5)
    first = verifier.run(max_probes=4)
    assert service.dispatcher.tasks[key]["state"] == "VERIFYING"
    verifier.run(max_probes=4, after=first[-1]["key"])
    assert service.dispatcher.tasks[key]["state"] == "DONE"


@pytest.mark.parametrize("limit", [True, 1, 101, 1.5])
def test_invalid_pass_budget_never_contacts_service(monkeypatch, limit):
    monkeypatch.setattr(verifier, "request", lambda *args: pytest.fail("contacted service"))
    with pytest.raises((ValueError, TypeError), match="max_probes"):
        verifier.run(max_probes=limit)


def test_server_recheck_respects_its_budget(service, tmp_path):
    key = add(service, tmp_path, 1)
    service.dispatcher.tasks[key]["evidence"].append("file second.txt")
    with pytest.raises(Refused, match="probe budget exhausted"):
        request("POST", "await/close", {"key": key, "by": "probe", "max_probes": 1})
    assert service.dispatcher.tasks[key]["state"] == "VERIFYING"


class StopAfter:
    def __init__(self, cycles):
        self.cycles = cycles
        self.waits = []

    def is_set(self):
        return False

    def wait(self, seconds):
        self.waits.append(seconds)
        return len(self.waits) >= self.cycles


def test_watch_uses_ten_minute_default_and_rotates_cursor(monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return [{"key": f"BL-{len(calls)}", "probe_count": 1}]

    monkeypatch.setattr(verifier, "run", run)
    stop = StopAfter(3)
    assert len(list(verifier.watch(stop=stop))) == 3
    assert stop.waits == [600, 600, 600]
    assert calls == [
        {"max_probes": 100, "after": None},
        {"max_probes": 100, "after": "BL-1"},
        {"max_probes": 100, "after": "BL-2"},
    ]


def test_watch_waits_after_service_failure(monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise Unavailable("serve down")
        return []

    monkeypatch.setattr(verifier, "run", run)
    stop = StopAfter(2)
    outcomes = list(verifier.watch(interval=600, stop=stop))
    assert "serve down" in outcomes[0][0]["error"]
    assert stop.waits == [600, 600]
    assert len(calls) == 2


def test_preexisting_stop_does_not_run(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(verifier, "run", lambda **kwargs: pytest.fail("unexpected pass"))
    assert list(verifier.watch(stop=stop)) == []


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_watch_rejects_invalid_interval(monkeypatch, interval):
    monkeypatch.setattr(verifier, "run", lambda **kwargs: pytest.fail("unexpected pass"))
    with pytest.raises(ValueError, match="interval"):
        list(verifier.watch(interval=interval))


def test_verify_cli_returns_one_json_result(service, tmp_path, capsys):
    add(service, tmp_path, 1)
    capsys.readouterr()
    assert cli.main(["await", "verify", "--max-probes", "2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]["closed"]
    assert result[0]["probe_count"] == 2


def test_readme_verify_help_is_offline(monkeypatch, capsys):
    monkeypatch.setattr(verifier, "request", lambda *args: pytest.fail("contacted service"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["await", "verify", "--help"])
    assert exc.value.code == 0
    assert "--max-probes" in capsys.readouterr().out
