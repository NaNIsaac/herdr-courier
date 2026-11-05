from types import SimpleNamespace

import pytest

from herdr_courier import config, daemon, mailbox
from herdr_courier.core.queue import Dispatcher, DispatchError
from herdr_courier.core.transport import HerdrError


@pytest.fixture
def configured(herdr, state_dir, monkeypatch):
    for name in ("boss", "lead", "observer", "w1", "w2"):
        herdr.add(name, name)
    monkeypatch.setattr(herdr, "server_socket", lambda: "test-socket", raising=False)
    monkeypatch.setattr(daemon, "CliHerdr", lambda: herdr)
    monkeypatch.setattr(daemon, "adopt_old_state", lambda: None)
    captured = []

    def capture(*args, **kwargs):
        captured.append(Dispatcher(*args, **kwargs))
        raise HerdrError("test_stop", "stop before starting any service")

    monkeypatch.setattr(daemon, "Dispatcher", capture)

    def build(settings):
        (state_dir / "config.toml").write_text("[ledger]\n" + settings, encoding="utf-8")
        assert daemon.serve(SimpleNamespace(transport="agent")) == 1
        return captured[-1]

    return build


def send_task(dispatcher, tmp_path):
    dispatcher.send_task(
        "w1",
        "task",
        source="lead",
        task={"key": "BL-1", "evidence": ["file missing.txt"], "cwd": str(tmp_path)},
    )


def test_configured_controller_and_execution_check_off(configured, tmp_path):
    dispatcher = configured('controller = "lead"\ncheck_execution = false\n')
    send_task(dispatcher, tmp_path)
    with pytest.raises(DispatchError, match="controller pane identity"):
        dispatcher.reassign_task("BL-1", "w2", sender_pane="boss")
    result = dispatcher.reassign_task("BL-1", "w2", sender_pane="lead")
    assert result["to"] == "w2"


def test_configured_notice_receiver(configured, tmp_path):
    dispatcher = configured('controller = "lead"\nnotify_to = "observer"\n')
    send_task(dispatcher, tmp_path)
    dispatcher.reassign_task("BL-1", "w2", sender_pane="lead", force=True, reason="recovery")
    assert any(
        "recovery" in item.text for item in mailbox.load(dispatcher.journal.parent, "observer")
    )
    assert not mailbox.load(dispatcher.journal.parent, "secretary")
    assert dispatcher.set_route("w1", "w2", sender_pane="observer") == {"w1": "w2"}


def test_notices_can_be_disabled_without_blocking_reassignment(configured, tmp_path):
    dispatcher = configured(
        'controller = "lead"\nnotify_on_override = false\nnotify_on_capacity = false\n'
    )
    send_task(dispatcher, tmp_path)
    for target in ("w2", "w1"):
        result = dispatcher.reassign_task(
            "BL-1", target, sender_pane="lead", force=True, reason="recovery"
        )
        assert "secretary_notice" not in result
    dispatcher._capacity_notice("limit", "capacity warning")
    assert not dispatcher.ledger_notices
    assert not mailbox.load(dispatcher.journal.parent, "secretary")
    assert not any(item.source == "courier" for item in dispatcher.items.values())


def test_execution_check_off_still_refuses_working_receiver(configured, herdr, tmp_path):
    dispatcher = configured('controller = "lead"\ncheck_execution = false\n')
    send_task(dispatcher, tmp_path)
    herdr.panes["w1"].status = "working"
    with pytest.raises(DispatchError, match="w1 working"):
        dispatcher.reassign_task("BL-1", "w2", sender_pane="lead")


def test_membership_schema_is_configurable(configured, tmp_path, monkeypatch):
    path = tmp_path / "members.json"
    path.write_text('{"members": ["w2"]}', encoding="utf-8")
    monkeypatch.setenv("COURIER_ROLES_FILE", str(path))
    dispatcher = configured('membership_field = "members"\n')
    send_task(dispatcher, tmp_path)
    dispatcher.check_tasks()
    assert "stale" in dispatcher.tasks["BL-1"]["flags"]


@pytest.mark.parametrize("key", ["notify_on_override", "notify_on_capacity", "check_execution"])
def test_check_switches_require_booleans(state_dir, key):
    path = state_dir / "config.toml"
    path.write_text(f'[ledger]\n{key} = "false"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="must be true or false"):
        config.load(path)
