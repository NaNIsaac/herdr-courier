import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from herdr_courier import cli, ledger


@pytest.fixture
def workers(herdr, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("boss", "w1", "w2", "w3", "w4"):
        herdr.add(name, name)
    get = herdr.agent_get

    def proven(target):
        return get(target) | {"lane_state_task": None, "descendants_working": False}

    monkeypatch.setattr(herdr, "agent_get", proven)
    return herdr


def send(key="BL-12", owner="w1", *extra):
    return cli.main(
        [
            "send",
            owner,
            "task",
            "--await",
            key,
            "--source",
            "boss",
            "--evidence",
            "file missing.txt",
            *extra,
        ]
    )


def reassign(*extra):
    return cli.main(["await", "reassign", "BL-12", "--to", "w2", "--source", "boss", *extra])


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert predicate()


def refused(action, text, capsys):
    with pytest.raises(SystemExit) as exc:
        action()
    assert exc.value.code == cli.EXIT_FAILED
    assert text in capsys.readouterr().err


def drain(workers, name, count):
    workers.panes[name].status = "idle"
    wait_for(lambda: len(workers.panes[name].submitted) == count)
    wait_for(lambda: not cli.request("GET", "status")["status"]["panes"][name]["pending"])


def test_c1_reminder_runs_once(workers, start):
    _, service = start(workers)
    assert send() == 0
    drain(workers, "w1", 1)
    assert send() == 0
    drain(workers, "w1", 2)
    assert workers.panes["w1"].submitted == ["task", "REMINDER BL-12, ignore if running"]
    assert len(service.dispatcher.tasks) == 1


def test_c2_reminder_bounded(workers, start, monkeypatch):
    _, service = start(workers)
    assert send() == 0
    drain(workers, "w1", 1)
    assert send() == 0
    drain(workers, "w1", 2)
    task = service.dispatcher.tasks["BL-12"]
    last = task["reminded_at"]
    monkeypatch.setattr(ledger.time, "time", lambda: last + 60)
    assert send() == send() == 0
    assert workers.panes["w1"].submitted == ["task", "REMINDER BL-12, ignore if running"]
    assert task["reminders_suppressed"] == 2
    journal = [json.loads(line) for line in service.dispatcher.journal.read_text().splitlines()]
    assert (
        len([line for line in journal if line.get("reason", "").startswith("reminder suppressed")])
        == 2
    )
    service.stop()
    _, service = start(workers)
    task = service.dispatcher.tasks["BL-12"]
    assert send() == 0
    assert task["reminders_suppressed"] == 3
    monkeypatch.setattr(ledger.time, "time", lambda: last + 1801)
    assert send() == 0
    drain(workers, "w1", 3)
    assert workers.panes["w1"].submitted.count("task") == 1
    assert workers.panes["w1"].submitted.count("REMINDER BL-12, ignore if running") == 2


def test_c3_same_key_other_owner(workers, start, capsys):
    _, service = start(workers)
    assert send() == 0
    refused(lambda: send(owner="w2"), "owned by w1 since", capsys)
    assert not workers.panes["w2"].submitted
    assert len(service.dispatcher.tasks) == 1


def test_c4_same_place_other_key(workers, start, capsys):
    _, service = start(workers)
    place = ["--repo", "games23", "--branch", "fix/x"]
    assert send("BL-12", "w1", *place) == 0
    refused(lambda: send("BL-13", "w1", *place), "open as BL-12", capsys)
    assert len(service.dispatcher.tasks) == 1
    assert len(service.dispatcher.items) == 1


def test_c5_race_two_clients(workers, start, tmp_path):
    _, service = start(workers)
    script = tmp_path / "sender.py"
    script.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "from herdr_courier.cli import main\n"
        "root = Path(sys.argv[1])\n"
        "owner = sys.argv[2]\n"
        "(root / (owner + '.ready')).touch()\n"
        "while not (root / 'go').exists():\n"
        "    time.sleep(0.01)\n"
        "raise SystemExit(main(['send', owner, 'task', '--await', 'BL-12', "
        "'--source', 'boss', '--evidence', 'file missing.txt']))\n",
        encoding="utf-8",
    )
    processes = []
    gate = threading.Event()

    def thread_send(owner):
        assert gate.wait(10)
        try:
            return send(owner=owner)
        except SystemExit as exc:
            return exc.code

    try:
        for owner in ("w3", "w4"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, str(script), str(tmp_path), owner],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=os.environ | {"COURIER_STATE_DIR": str(tmp_path)},
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(thread_send, owner) for owner in ("w1", "w2")]
            wait_for(lambda: all((tmp_path / f"{owner}.ready").exists() for owner in ("w3", "w4")))
            (tmp_path / "go").touch()
            gate.set()
            codes = [future.result(timeout=10) for future in futures]
        for process in processes:
            _, error = process.communicate(timeout=10)
            codes.append(process.returncode)
            if process.returncode:
                assert "owned by" in error
    finally:
        gate.set()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert sorted(codes) == [0, 1, 1, 1]
    wait_for(lambda: sum(len(p.submitted) for p in workers.panes.values()) == 1)
    assert len(service.dispatcher.tasks) == 1
    assert len(service.dispatcher.items) == 1
    task = service.dispatcher.tasks["BL-12"]
    assert workers.panes[task["to"]].submitted == ["task"]


def test_c6_reopen_after_done(workers, start, capsys):
    _, service = start(workers)
    assert send() == 0
    drain(workers, "w1", 1)
    d = service.dispatcher
    with d.lock:
        # Closing belongs to pilot B; seed its terminal result as this test's precondition.
        task = d.tasks["BL-12"]
        task["state"] = "DONE"
        task["closed_by"] = "probe file"
        d._task_record(task)
    service.stop()
    _, service = start(workers)
    d = service.dispatcher
    refused(send, "closed key requires --reopen", capsys)
    assert send("BL-12", "w1", "--reopen") == 0
    assert d.tasks["BL-12"]["attempts"] == 1
    assert d.tasks["BL-12"]["state"] == "OPEN"
    drain(workers, "w1", 2)
    assert workers.panes["w1"].submitted == ["task", "task"]


@pytest.mark.parametrize("proof", ["working", "descendant", "lane-state", "unavailable"])
def test_c7_reassign_working(workers, start, capsys, monkeypatch, proof):
    _, service = start(workers)
    assert send() == 0
    drain(workers, "w1", 1)
    if proof != "working":
        workers.panes["w1"].status = "idle"
        get = workers.agent_get

        def proven(target):
            agent = get(target)
            if target == "w1":
                if proof == "descendant":
                    agent["descendants_working"] = True
                elif proof == "lane-state":
                    agent["lane_state_task"] = "BL-12"
                else:
                    agent.pop("lane_state_task")
            return agent

        monkeypatch.setattr(workers, "agent_get", proven)
    reason = {
        "working": "w1 working",
        "descendant": "w1 working",
        "lane-state": "lane-state still owns BL-12",
        "unavailable": "execution proof unavailable",
    }[proof]
    refused(reassign, reason, capsys)
    assert service.dispatcher.tasks["BL-12"]["to"] == "w1"
    assert not workers.panes["w2"].submitted


def test_c8_reassign_idle_clean(workers, start, tmp_path):
    _, service = start(workers)
    assert send("BL-12", "w1", "--cwd", str(tmp_path)) == 0
    drain(workers, "w1", 1)
    workers.panes["w1"].status = "idle"
    assert reassign() == 0
    task = service.dispatcher.tasks["BL-12"]
    assert task["to"] == "w2"
    assert task["attempts"] == 1
    assert task["previous_owners"] == ["w1"]
    assert workers.agent_get("w1")["agent_session"]["value"] == "s1"
    service.stop()
    _, service = start(workers)
    assert service.dispatcher.tasks["BL-12"]["to"] == "w2"
    assert service.dispatcher.tasks["BL-12"]["previous_owners"] == ["w1"]


@pytest.mark.parametrize("evidence", ["file", "loose-branch", "packed-branch"])
def test_c9_reassign_evidence_exists(workers, start, tmp_path, capsys, evidence):
    _, service = start(workers)
    assert send("BL-12", "w1", "--cwd", str(tmp_path), "--branch", "fix/x") == 0
    drain(workers, "w1", 1)
    workers.panes["w1"].status = "idle"
    if evidence == "file":
        (tmp_path / "missing.txt").write_text("output")
    elif evidence == "loose-branch":
        refs = tmp_path / ".git" / "refs" / "heads" / "fix"
        refs.mkdir(parents=True)
        (refs / "x").write_text("a" * 40)
    else:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "packed-refs").write_text("a" * 40 + " refs/heads/fix/x\n")
    refused(reassign, "evidence exists: verify first", capsys)
    assert service.dispatcher.tasks["BL-12"]["to"] == "w1"


def test_c10_side_effect_never_retried(workers, start):
    _, service = start(workers)
    assert (
        cli.main(
            [
                "send",
                "w1",
                "task",
                "--await",
                "BL-12",
                "--source",
                "boss",
                "--evidence",
                "merge games23#5",
            ]
        )
        == 0
    )
    drain(workers, "w1", 1)
    assert cli.main(["send", "boss", "FAILED BL-12", "--source", "w1"]) == 0
    d = service.dispatcher
    for _ in range(3):
        d.tick()
    flags = [item for item in d.items.values() if item.source == "courier"]
    assert len(flags) == 1
    assert flags[0].target == "boss"
    assert d.tasks["BL-12"]["state"] == "FAILED"
    assert d.tasks["BL-12"]["attempts"] == 0
    assert workers.panes["w1"].submitted == ["task"]


def test_c11_ownership_vs_execution(workers, start, monkeypatch):
    _, service = start(workers)
    assert send() == 0
    drain(workers, "w1", 1)
    workers.panes["w1"].status = "idle"
    task = service.dispatcher.tasks["BL-12"]
    now = task["sent"] + 3600
    monkeypatch.setattr(ledger.time, "time", lambda: now)
    service.dispatcher.tick()
    assert task["to"] == "w1"
    assert task["state"] == "OPEN"
    assert task["attempts"] == 0
    assert not workers.panes["w2"].submitted


def test_reassign_cancels_original_queued_delivery(workers, start):
    workers.panes["w1"].box = "human typing"
    _, service = start(workers)
    assert send() == 0
    assert reassign() == 0
    assert service.dispatcher.tasks["BL-12"]["to"] == "w2"
    workers.panes["w1"].box = ""
    for _ in range(3):
        service.dispatcher.tick()
    assert not workers.panes["w1"].submitted
    assert not workers.panes["w2"].submitted
    assert all(item.state == "cancelled" for item in service.dispatcher.items.values())


def test_reassign_force_is_recorded(workers, start):
    _, service = start(workers)
    assert send() == 0
    drain(workers, "w1", 1)
    assert reassign("--force") == 0
    task = service.dispatcher.tasks["BL-12"]
    assert "w1 working" in task["override"]
    assert task["reassigned_by"] == "boss"


def test_reassign_requires_boss(workers, start, capsys):
    _, service = start(workers)
    assert send() == 0
    refused(
        lambda: cli.main(["await", "reassign", "BL-12", "--to", "w2", "--source", "w1"]),
        "reassign requires boss",
        capsys,
    )
    assert service.dispatcher.tasks["BL-12"]["to"] == "w1"


def test_reassign_unknown_evidence_is_refused(workers, start, capsys):
    _, service = start(workers)
    assert (
        cli.main(
            [
                "send",
                "w1",
                "task",
                "--await",
                "BL-12",
                "--source",
                "boss",
                "--evidence",
                "pr games23#5",
            ]
        )
        == 0
    )
    drain(workers, "w1", 1)
    workers.panes["w1"].status = "idle"
    refused(reassign, "cannot check evidence locally", capsys)
    assert service.dispatcher.tasks["BL-12"]["to"] == "w1"
