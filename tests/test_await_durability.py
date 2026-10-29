import io
import json
import time

import pytest

from herdr_courier import cli, ledger


def send(key="BL-12", owner="w1", *extra):
    return cli.main(
        [
            "send",
            owner,
            "task",
            "--await",
            key,
            "--evidence",
            "file out/a.txt",
            "--source",
            "boss",
            *extra,
        ]
    )


def listed(capsys):
    capsys.readouterr()
    assert cli.main(["await", "list", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["tasks"]


@pytest.fixture
def workers(herdr):
    for name in ("boss", "w1", "w2", "w9"):
        herdr.add(name, name)
    return herdr


def delivered(pane, expected):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(pane.submitted) < expected:
        time.sleep(0.02)
    assert len(pane.submitted) == expected


def test_a1_open_survives_restart(workers, start, capsys):
    _, service = start(workers)
    assert send() == 0
    before = listed(capsys)
    service.stop()
    start(workers)
    assert listed(capsys) == before
    assert before[0]["state"] == "OPEN"
    assert before[0]["attempts"] == 0


def test_a2_boss_context_loss(workers, start, capsys):
    start(workers)
    assert send() == 0
    assert send("BL-13", "w2") == 0
    capsys.readouterr()
    assert cli.main(["await", "list"]) == 0
    output = capsys.readouterr().out
    assert all(s in output for s in ("BL-12", "BL-13", "age=", "due="))


def test_a3_compact_hook_text(workers, start, tmp_path, monkeypatch, capsys):
    _, service = start(workers)
    assert send() == 0
    assert send("BL-13", "w2") == 0
    (tmp_path / "config.toml").write_text(
        '[mailbox]\nreaders = ["boss"]\n[ledger]\nresume_for = ["boss"]\n'
    )
    for stopped in (False, True):
        if stopped:
            service.stop()
        monkeypatch.setattr(
            "sys.stdin", io.StringIO('{"hook_event_name":"SessionStart","source":"compact"}')
        )
        capsys.readouterr()
        assert cli.main(["inbox", "boss", "--hook"]) == 0
        text = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert all(s in text for s in ("BL-12", "BL-13", "owner=w1", "owner=w2"))
        if stopped:
            assert "ledger read from file, serve down" in text
        assert "nothing open" not in text


def test_a4_await_list_serve_down(workers, start, tmp_path, capsys):
    _, service = start(workers)
    assert send() == 0
    service.stop()
    path = tmp_path / "journal.jsonl"
    before = path.read_bytes()
    capsys.readouterr()
    assert cli.main(["await", "list"]) == 0
    assert "ledger read from file, serve down" in capsys.readouterr().out
    assert path.read_bytes() == before


def test_a5_await_list_unreadable(workers, start, tmp_path, capsys):
    _, service = start(workers)
    assert send() == 0
    service.stop()
    (tmp_path / "journal.jsonl").rename(tmp_path / "unreadable")
    capsys.readouterr()
    assert cli.main(["await", "list"]) != 0
    output = capsys.readouterr().out
    assert "ledger unavailable" in output
    assert "nothing open" not in output


def test_a6_lost_contact_retry(workers, start, monkeypatch):
    _, service = start(workers)
    real = cli.request
    lost = False

    def lossy(method, path, body=None, timeout=10):
        nonlocal lost
        reply = real(method, path, body, timeout)
        if path == "send" and not lost:
            lost = True
            raise cli.LostContact("reply lost")
        return reply

    monkeypatch.setattr(cli, "request", lossy)
    assert send() == 0
    delivered(workers.panes["w1"], 1)
    assert len(service.dispatcher.tasks) == 1
    assert len(service.dispatcher.items) == 1


def test_a7_open_across_generations(workers, start, capsys):
    _, service = start(workers)
    assert send() == 0
    delivered(workers.panes["w1"], 1)
    for _ in range(12):
        service.stop()
        _, service = start(workers)
    assert [t["key"] for t in listed(capsys)] == ["BL-12"]
    assert not service.dispatcher.items
    assert workers.panes["w1"].submitted == ["task"]


def test_a8_manual_path_replay(workers, start, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        send("BL-14")
    assert exc.value.code == cli.EXIT_UNAVAILABLE
    assert "herdr agent prompt" in capsys.readouterr().err
    path = tmp_path / "pending-dispatch.jsonl"
    assert len(path.read_text().splitlines()) == 1
    _, service = start(workers)
    assert service.dispatcher.tasks["BL-14"]["note"] == "manual"
    assert service.dispatcher.tasks["BL-14"]["state"] == "OPEN"
    assert not service.dispatcher.items
    assert not workers.panes["w1"].submitted
    assert not path.exists()
    assert len(list(tmp_path.glob("pending-dispatch.*.replayed.jsonl"))) == 1


def test_a9_done_from_owner_only(workers, start):
    _, service = start(workers)
    d = service.dispatcher
    logs = []
    d.log = logs.append
    assert send() == 0
    assert cli.main(["send", "boss", "DONE BL-12", "--source", "w2"]) == 0
    assert d.tasks["BL-12"]["state"] == "OPEN"
    assert any("non-owner" in line for line in logs)
    assert cli.main(["send", "boss", "DONE BL-12", "--source", "w1"]) == 0
    assert d.tasks["BL-12"]["state"] == "VERIFYING"
    count = len(
        [
            line
            for line in d.journal.read_text().splitlines()
            if json.loads(line).get("event") == "task"
        ]
    )
    assert cli.main(["send", "boss", "DONE BL-12", "--source", "w1"]) == 0
    assert count == len(
        [
            line
            for line in d.journal.read_text().splitlines()
            if json.loads(line).get("event") == "task"
        ]
    )


def test_a10_overdue_flags_sender_only(workers, start, monkeypatch):
    _, service = start(workers)
    assert send() == 0
    service.stop()
    d = service.dispatcher
    task = d.tasks["BL-12"]
    task["due"] = task["sent"] + 100
    monkeypatch.setattr(ledger.time, "time", lambda: task["sent"] + 101)
    d.check_tasks()
    monkeypatch.setattr(ledger.time, "time", lambda: task["sent"] + 201)
    d.check_tasks()
    d.check_tasks()
    flags = [i for i in d.items.values() if i.text.startswith("OVERDUE")]
    assert [i.text for i in flags] == ["OVERDUE BL-12 1x", "OVERDUE BL-12 2x"]
    assert all(i.target == "boss" for i in flags)
    assert task["state"] == "OPEN"


def test_a11_stale_open_flagged_once(workers, start, monkeypatch, tmp_path):
    _, service = start(workers)
    assert send() == 0
    assert send("BL-13", "w9") == 0
    service.stop()
    d = service.dispatcher
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"lanes": {"boss": {}, "w1": {}, "w2": {}}}))
    monkeypatch.setenv("COURIER_ROLES_FILE", str(roles))
    d.check_tasks()
    assert [i.text for i in d.items.values() if i.text.startswith("STALE")] == ["STALE BL-13"]
    now = max(t["sent"] for t in d.tasks.values()) + 8 * 86400
    monkeypatch.setattr(ledger.time, "time", lambda: now)
    d.check_tasks()
    d.check_tasks()
    flags = [i for i in d.items.values() if i.text.startswith("STALE")]
    assert {i.text for i in flags} == {"STALE BL-12", "STALE BL-13"}
    assert len(flags) == 2
    assert all(i.target == "boss" for i in flags)


def test_a12_torn_last_line(workers, start, tmp_path, capsys):
    _, service = start(workers)
    assert send() == 0
    service.stop()
    with (tmp_path / "journal.jsonl").open("a") as stream:
        stream.write('{"event":"task","task":')
    start(workers)
    assert listed(capsys)[0]["key"] == "BL-12"
