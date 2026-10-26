import io
import json

import pytest
from conftest import call

from herdr_courier import cli, config, mailbox
from herdr_courier.state import journal_path


def send(server, service, text, **extra):
    body = {"target": "boss", "text": text, "source": "xr1"} | extra
    status, reply = call(server, "POST", "/api/dispatch/send", body, service.token)
    assert status == 200
    return reply["item"]


def journal_queued(root):
    lines = journal_path(root).read_text(encoding="utf-8").splitlines()
    records = map(json.loads, lines)
    return [r["id"] for r in records if r.get("event") == "item" and r["reason"] == "queued"]


def test_each_queued_item_is_copied_once_with_the_journal_id(herdr, start, tmp_path):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr, mailbox=True)
    first = send(server, service, "merge-ready RL#24", client_id="abcdef0123456789")
    send(server, service, "merge-ready RL#24", client_id="abcdef0123456789")
    second = send(server, service, "another")
    kept = mailbox.load(tmp_path, "boss")
    assert [m.id for m in kept] == [first["id"], second["id"]] == journal_queued(tmp_path)
    assert (kept[0].source, kept[0].text, kept[0].state) == ("xr1", "merge-ready RL#24", "new")


def test_no_copy_while_the_mailbox_is_off(herdr, start, tmp_path):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    send(server, service, "hello")
    assert not mailbox.mailbox_dir(tmp_path).exists()


class Item:
    def __init__(self, id, text, target="boss", source="xr1"):
        self.id, self.text, self.target, self.source = id, text, target, source
        self.created, self.human = 1_800_000_000.0, False


@pytest.fixture
def readers(state_dir):
    (state_dir / "config.toml").write_text('[mailbox]\nreaders = ["boss"]\n', encoding="utf-8")
    return state_dir


def hook(monkeypatch, capsys, event, source=None, name="boss"):
    payload = {"hook_event_name": event} | ({"source": source} if source else {})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["inbox", name, "--hook"]) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_hook_shows_each_message_once_and_again_after_compaction(readers, monkeypatch, capsys):
    mailbox.add(readers, Item("a1", "first"))
    mailbox.add(readers, Item("b2", "second"))
    shown = hook(monkeypatch, capsys, "UserPromptSubmit")
    assert shown["hookEventName"] == "UserPromptSubmit"
    assert "a1 from xr1" in shown["additionalContext"] and "second" in shown["additionalContext"]
    assert hook(monkeypatch, capsys, "UserPromptSubmit") is None

    assert cli.main(["inbox", "boss", "--ack", "a1"]) == 0
    capsys.readouterr()
    again = hook(monkeypatch, capsys, "SessionStart", "compact")
    assert "b2 from xr1" in again["additionalContext"] and "a1 " not in again["additionalContext"]
    assert [m.state for m in mailbox.load(readers, "boss")] == ["done", "read"]

    log = mailbox.hook_log_path(readers).read_text(encoding="utf-8").splitlines()
    assert [line.split()[2:4] for line in log] == [
        ["UserPromptSubmit", "shown=a1,b2"],
        ["UserPromptSubmit", "shown=-"],
        ["SessionStart/compact", "shown=b2"],
    ]
    assert log[1].endswith("bytes=0")


def test_hook_is_silent_for_an_agent_that_is_not_a_reader(readers, monkeypatch, capsys):
    mailbox.add(readers, Item("a1", "for xr1", target="xr1"))
    assert hook(monkeypatch, capsys, "UserPromptSubmit", name="xr1") is None
    assert not mailbox.hook_log_path(readers).exists()


def test_hook_without_readers_never_asks_herdr(state_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli, "own_name", lambda: pytest.fail("asked herdr for the name"))
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["inbox", "--hook"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_survives_a_broken_config(state_dir, monkeypatch, capsys):
    (state_dir / "config.toml").write_text("[mailbox]\nreaders = 1\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["inbox", "boss", "--hook"]) == 0
    assert "readers" in capsys.readouterr().err


def test_hook_cuts_long_text_and_points_to_the_full_one(readers, monkeypatch, capsys):
    mailbox.add(readers, Item("long1", "x" * (mailbox.HOOK_TEXT_CHARS + 50)))
    shown = hook(monkeypatch, capsys, "UserPromptSubmit")["additionalContext"]
    assert "courier inbox --show long1" in shown
    assert "x" * (mailbox.HOOK_TEXT_CHARS + 1) not in shown


def test_inbox_lists_open_messages_and_refuses_unknown_ids(state_dir, capsys):
    mailbox.add(state_dir, Item("a1", "first"))
    mailbox.add(state_dir, Item("b2", "second"))
    assert cli.main(["inbox", "boss", "--ack", "b2"]) == 0
    capsys.readouterr()
    assert cli.main(["inbox", "boss"]) == 0
    out = capsys.readouterr().out
    assert "a1 from xr1" in out and "b2" not in out
    assert cli.main(["inbox", "boss", "--all", "--json"]) == 0
    assert [m["state"] for m in json.loads(capsys.readouterr().out)] == ["new", "done"]
    assert cli.main(["inbox", "boss", "--ack", "zz"]) == cli.EXIT_FAILED
    assert "no message zz" in capsys.readouterr().err


def test_a_line_cut_short_by_a_crash_is_skipped(state_dir):
    mailbox.add(state_dir, Item("a1", "first"))
    with mailbox.path_for(state_dir, "boss").open("a", encoding="utf-8") as f:
        f.write('{"kind": "message", "id": "b')
    mailbox.add(state_dir, Item("c3", "third"))
    assert [m.id for m in mailbox.load(state_dir, "boss")] == ["a1", "c3"]


def test_pane_ids_make_safe_file_names(tmp_path):
    assert mailbox.path_for(tmp_path, "w3:pC").name == "w3_pc.jsonl"


@pytest.mark.parametrize("value", ["1", '"yes"'])
def test_mailbox_write_must_be_a_boolean(state_dir, value):
    (state_dir / "config.toml").write_text(f"[mailbox]\nwrite = {value}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="true or false"):
        config.load()


@pytest.mark.parametrize(
    ("text", "warned"),
    [
        ("C:/Program Files/Git/effort high", True),
        ("C:/Program Files/Git/inbox", True),
        ("/effort high", False),
        ("[xr1] merge-ready", False),
    ],
)
def test_send_warns_about_a_slash_git_bash_rewrote(herdr, start, capsys, text, warned):
    herdr.add("w1:p1", "boss")
    start(herdr)
    assert cli.main(["send", "boss", text]) == 0
    assert ("Git Bash" in capsys.readouterr().err) is warned
