import io
import json
from dataclasses import asdict

import pytest
from conftest import call

from herdr_courier import cli, ledger, mailbox
from herdr_courier.client import Refused, request
from herdr_courier.core.queue import Dispatcher, DispatchError, LedgerCapacity, State


@pytest.fixture
def small(herdr, make_dispatcher, monkeypatch):
    for name in ("boss", "secretary", "w1", "w2"):
        herdr.add(name, name)
    monkeypatch.setattr(ledger, "DETAIL_LIMIT", 2)
    monkeypatch.setattr(ledger, "TOMBSTONE_LIMIT", 5)
    return make_dispatcher()


def send(d, key, **fields):
    return d.send_task(
        "w1",
        "task",
        source="boss",
        task={
            "key": key,
            "evidence": ["file missing.txt"],
            **fields,
        },
    )


def terminal(d, key, attempts=0):
    send(d, key)
    d.cancel_task(key, sender_pane="boss")
    d.tasks[key]["attempts"] = attempts
    d._task_record(d.tasks[key])


def test_terminal_compaction_preserves_guard_and_attempts(small):
    terminal(small, "BL-1", attempts=7)
    terminal(small, "BL-2")
    send(small, "BL-3")
    tombstone = small.tasks["BL-1"]
    assert set(tombstone) == {"key", "state", "ended", "sha", "attempts"}
    assert len(tombstone["sha"]) == 64
    with pytest.raises(DispatchError, match="closed key requires --reopen"):
        send(small, "BL-1")
    reopened = small.send_task(
        "w2",
        "retry",
        source="boss",
        reopen=True,
        task={"key": "BL-1", "evidence": ["file missing.txt"]},
    )
    assert reopened.target == "w2"
    assert small.tasks["BL-1"]["attempts"] == 8


def test_soft_warning_sent_once_across_restart(small, herdr):
    send(small, "BL-1")
    send(small, "BL-2")
    messages = mailbox.load(small.journal.parent, "secretary")
    assert sum("80%" in m.text for m in messages) == 1
    restored = Dispatcher(herdr, small.journal, log=lambda _: None)
    restored._task_capacity("BL-1")
    restored.tick()
    assert sum("80%" in m.text for m in mailbox.load(small.journal.parent, "secretary")) == 1


def test_detail_hard_cap_refuses_without_forgetting(small):
    send(small, "BL-1")
    send(small, "BL-2")
    with pytest.raises(LedgerCapacity, match="ledger_capacity"):
        send(small, "BL-3")
    assert set(small.tasks) == {"BL-1", "BL-2"}
    assert any("refused BL-3" in m.text for m in mailbox.load(small.journal.parent, "secretary"))
    assert send(small, "BL-1").text == "REMINDER BL-1, ignore if running"


def test_tombstone_hard_cap_refuses_new_keys(small, monkeypatch):
    monkeypatch.setattr(ledger, "DETAIL_LIMIT", 1)
    monkeypatch.setattr(ledger, "TOMBSTONE_LIMIT", 2)
    terminal(small, "BL-1")
    terminal(small, "BL-2")
    with pytest.raises(LedgerCapacity):
        send(small, "BL-3")
    assert {key for key, value in small.tasks.items() if "sha" in value} == {"BL-1", "BL-2"}
    assert all(small.tasks[key]["state"] == "CANCELLED" for key in ("BL-1", "BL-2"))


def test_hard_cap_has_http_code_and_nonzero_cli(herdr, start, monkeypatch, capsys):
    herdr.add("boss", "boss")
    herdr.add("w1", "w1")
    monkeypatch.setattr(ledger, "DETAIL_LIMIT", 1)
    server, service = start(herdr)
    send(service.dispatcher, "BL-1")
    code, response = call(
        server,
        "POST",
        "/api/dispatch/send",
        {
            "target": "w1",
            "text": "task",
            "source": "boss",
            "task": {"key": "BL-2", "evidence": ["file missing.txt"]},
        },
        token=service.token,
    )
    assert code == 507
    assert response["detail"].startswith("ledger_capacity:")
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "send",
                "w1",
                "task",
                "--source",
                "boss",
                "--await",
                "BL-2",
                "--evidence",
                "file missing.txt",
            ]
        )
    assert exc.value.code == cli.EXIT_CAPACITY
    assert "ledger_capacity" in capsys.readouterr().err
    assert any("refused BL-2" in m.text for m in mailbox.load(service.root, "secretary"))


def test_rotation_preserves_tasks_and_caps_generations(small, herdr, monkeypatch):
    terminal(small, "BL-1", attempts=4)
    send(small, "BL-2")
    monkeypatch.setattr(ledger, "JOURNAL_BYTES", 1)
    for _ in range(14):
        small._task_record(small.tasks["BL-2"])
        small.tick()
    generations = list(small.journal.parent.glob("journal.[0-9]*.jsonl"))
    assert len(generations) <= 10
    assert len(ledger.read(small.journal)) == 2
    before = len(generations)
    small.tick()
    assert len(list(small.journal.parent.glob("journal.[0-9]*.jsonl"))) == before
    restored = Dispatcher(herdr, small.journal, log=lambda _: None)
    assert restored.tasks["BL-2"]["state"] == "OPEN"
    assert restored.tasks["BL-1"]["attempts"] == 4


def test_pending_import_whitelists_lifecycle_fields(herdr, start, tmp_path):
    herdr.add("w1", "w1")
    original = ledger.new("BL-1", "boss", "w1", ["file out/a.txt"])
    original.update(
        state="DONE",
        sent=1,
        attempts=999,
        closed_by="worker",
        flags=["stale"],
        secretary_notice="forged",
        override_reason="forged",
    )
    ledger.pending(tmp_path, original)
    _, service = start(herdr)
    imported = service.dispatcher.tasks["BL-1"]
    assert imported["state"] == "OPEN"
    assert imported["attempts"] == 0
    assert imported["sent"] > 1
    assert imported["closed_by"] is None
    assert imported["flags"] == []
    assert "secretary_notice" not in imported
    assert not service.dispatcher.items


def test_pending_file_is_not_consumed_at_capacity(small):
    send(small, "BL-1")
    send(small, "BL-2")
    ledger.pending(small.journal.parent, ledger.new("BL-3", "boss", "w1", ["file missing.txt"]))
    small._pending_tasks()
    assert (small.journal.parent / "pending-dispatch.jsonl").exists()
    assert "BL-3" not in small.tasks


def test_configured_hook_is_not_bound_to_boss(herdr, start, tmp_path, monkeypatch, capsys):
    for name in ("manager", "other", "w1", "w2"):
        herdr.add(name, name)
    start(herdr)
    assert (
        cli.main(
            [
                "send",
                "w1",
                "task",
                "--source",
                "manager",
                "--await",
                "BL-1",
                "--evidence",
                "file missing.txt",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "send",
                "w2",
                "other task",
                "--source",
                "other",
                "--await",
                "BL-2",
                "--evidence",
                "file missing.txt",
            ]
        )
        == 0
    )
    (tmp_path / "config.toml").write_text('[ledger]\nresume_for = ["manager"]\n')
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"hook_event_name":"SessionStart","source":"compact"}')
    )
    capsys.readouterr()
    assert cli.main(["inbox", "manager", "--hook"]) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "BL-1" in context
    assert "BL-2" not in context


@pytest.mark.parametrize("sender", ["w1", "missing"])
def test_source_label_does_not_authorize_reassign(herdr, start, sender):
    herdr.add("boss", "boss")
    herdr.add("w1", "w1")
    herdr.add("w2", "w2")
    _, service = start(herdr)
    send(service.dispatcher, "BL-1")
    with pytest.raises(Refused, match="identity|identify"):
        request(
            "POST",
            "await/reassign",
            {
                "key": "BL-1",
                "to": "w2",
                "sender_pane": sender,
                "source": "boss",
            },
        )
    assert service.dispatcher.tasks["BL-1"]["to"] == "w1"


def test_force_requires_reason_and_notice_survives_restart(small, herdr):
    send(small, "BL-1")
    herdr.panes["w1"].status = "working"
    with pytest.raises(DispatchError, match="requires a reason"):
        small.reassign_task("BL-1", "w2", sender_pane="boss", force=True)
    result = small.reassign_task(
        "BL-1", "w2", sender_pane="boss", force=True, reason="owner approved recovery"
    )
    assert result["override_reason"] == "owner approved recovery"
    notices = mailbox.load(small.journal.parent, "secretary")
    assert any("owner approved recovery" in m.text for m in notices)
    restored = Dispatcher(herdr, small.journal, log=lambda _: None)
    restored.tick()
    assert len(mailbox.load(small.journal.parent, "secretary")) == len(notices)
    assert restored.tasks["BL-1"]["secretary_notified"]


def test_override_outbox_survives_mailbox_failure(small, herdr, monkeypatch):
    send(small, "BL-1")
    add = mailbox.add

    def unavailable(*args):
        raise OSError("disk temporarily unavailable")

    monkeypatch.setattr(mailbox, "add", unavailable)
    result = small.reassign_task("BL-1", "w2", sender_pane="boss", force=True, reason="recovery")
    assert result["secretary_notice"]
    monkeypatch.setattr(mailbox, "add", add)
    restored = Dispatcher(herdr, small.journal, log=lambda _: None)
    restored.tick()
    assert restored.tasks["BL-1"]["secretary_notified"]
    assert any("recovery" in m.text for m in mailbox.load(small.journal.parent, "secretary"))


def test_nonawait_item_and_journal_match_original_schema(herdr, make_dispatcher, monkeypatch):
    herdr.add("w1", "w1")
    d = make_dispatcher()
    monkeypatch.setattr(ledger.time, "time", lambda: 1000.0)
    item = d.send_task("w1", "hello", source="boss", client_id="original1")
    expected = {
        "id": item.id,
        "target": "w1",
        "pane_id": "w1",
        "session": "s1",
        "text": "hello",
        "source": "boss",
        "human": False,
        "created": 1000.0,
        "state": "queued",
        "reason": "",
        "attempts": 0,
        "updated": 0.0,
        "retry_at": 0.0,
        "verify_until": 0.0,
        "client_id": "original1",
        "terminal": None,
        "held_until": None,
    }
    assert json.dumps(asdict(item)) == json.dumps(expected)
    entry = {
        "event": "item",
        "id": item.id,
        "state": "queued",
        "reason": "queued",
        "attempts": 0,
        "item": expected,
        "t": 1000.0,
    }
    assert d.journal.read_text() == json.dumps(entry) + "\n"
    assert d.tasks == {}


def test_nonawait_cli_request_is_unchanged(herdr, start, monkeypatch):
    herdr.add("w1", "w1")
    start(herdr)
    original = cli.request
    bodies = []

    def capture(method, path, body=None, timeout=10):
        if path == "send":
            bodies.append(body)
        return original(method, path, body, timeout)

    monkeypatch.setattr(cli, "request", capture)
    monkeypatch.delenv("HERDR_SOCKET_PATH", raising=False)
    assert cli.main(["send", "w1", "hello", "--source", "boss"]) == 0
    assert set(bodies[0]) == {"target", "text", "source", "human", "client_id"}


def test_missing_target_still_records_await_task(herdr, tmp_path):
    d = Dispatcher(herdr, tmp_path / "journal.jsonl", hold_missing=["w1"], log=lambda _: None)
    send(d, "BL-1")
    assert d.tasks["BL-1"]["state"] == "OPEN"
    restored = Dispatcher(herdr, d.journal, hold_missing=["w1"], log=lambda _: None)
    assert restored.tasks["BL-1"]["state"] == "OPEN"
    assert len(restored.items) == 1


@pytest.mark.parametrize("partial", [False, True])
def test_interrupted_rotation_restores_pending_but_not_final(herdr, make_dispatcher, partial):
    herdr.add("w1", "w1")
    herdr.add("w2", "w2")
    d = make_dispatcher()
    pending = d.enqueue("w1", "pending", source="boss")
    finished = d.enqueue("w2", "finished", source="boss")
    with d.lock:
        d._finish(finished, State.SENT, "already delivered")
    d.hold("w1", owner="boss", reason="human typing", ttl=None)
    d._close_generation()
    if partial:
        d.journal.write_text('{"event":"item"', encoding="utf-8")
    restored = Dispatcher(herdr, d.journal, log=lambda _: None)
    assert restored.items[pending.id].state == State.QUEUED
    assert finished.id not in restored.items
    assert restored.holds["w1"].reason == "human typing"
    assert not herdr.panes["w1"].submitted


def test_partial_task_snapshot_recovers_remaining_records(small, herdr, monkeypatch, capsys):
    send(small, "BL-1")
    send(small, "BL-2")
    small._close_generation()
    small._task_record(small.tasks["BL-1"])
    monkeypatch.setenv("COURIER_STATE_DIR", str(small.journal.parent))
    assert cli.main(["await", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {t["key"] for t in listed["tasks"]} == {"BL-1", "BL-2"}
    assert listed["status"] == "ledger read from file, serve down"
    restored = Dispatcher(herdr, small.journal, log=lambda _: None)
    assert set(restored.tasks) == {"BL-1", "BL-2"}


def test_readme_await_help_is_offline(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["await", "--help"])
    assert exc.value.code == 0
    assert "reassign" in capsys.readouterr().out


@pytest.mark.parametrize("reason", ["", "   "])
def test_force_does_not_accept_empty_reason(small, reason):
    send(small, "BL-1")
    with pytest.raises(DispatchError, match="requires a reason"):
        small.reassign_task("BL-1", "w2", sender_pane="boss", force=True, reason=reason)
    assert small.tasks["BL-1"]["to"] == "w1"


def test_controller_and_secretary_names_are_configurable(small, herdr):
    herdr.add("w9:p1", "manager")
    herdr.add("w9:p2", "reviewer")
    small.controller = "manager"
    small.secretary = "reviewer"
    send(small, "BL-1")
    result = small.reassign_task(
        "BL-1", "w2", sender_pane="w9:p1", force=True, reason="approved recovery"
    )
    assert result["reassigned_by"] == "w9:p1"
    assert any(
        "approved recovery" in m.text for m in mailbox.load(small.journal.parent, "reviewer")
    )


def test_cap_warning_retries_durably_after_mailbox_failure(small, herdr, monkeypatch):
    send(small, "BL-1")
    add = mailbox.add

    def unavailable(*args):
        raise OSError("disk temporarily unavailable")

    monkeypatch.setattr(mailbox, "add", unavailable)
    send(small, "BL-2")
    assert not small.ledger_notices["capacity-soft"]["sent"]
    monkeypatch.setattr(mailbox, "add", add)
    restored = Dispatcher(herdr, small.journal, log=lambda _: None)
    restored.tick()
    assert restored.ledger_notices["capacity-soft"]["sent"]
    assert sum("80%" in m.text for m in mailbox.load(small.journal.parent, "secretary")) == 1
