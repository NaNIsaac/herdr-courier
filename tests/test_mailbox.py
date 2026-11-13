import io
import json
import time

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


def test_messages_typed_in_as_one_prompt_keep_a_mailbox_row_each(herdr, start, tmp_path, capsys):
    pane = herdr.add("w1:p1", "boss", box="typing")
    server, service = start(herdr, mailbox=True)
    first = send(server, service, "first")
    second = send(server, service, "second")
    pane.box = ""
    deadline = time.monotonic() + 5
    while not pane.submitted and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(pane.submitted) == 1
    assert f"--- {first['id']} from xr1" in pane.submitted[0]
    assert f"--- {second['id']} from xr1" in pane.submitted[0]
    assert [m.id for m in mailbox.load(tmp_path, "boss")] == [first["id"], second["id"]]
    assert cli.main(["inbox", "boss", "--ack", second["id"]]) == 0
    capsys.readouterr()
    assert [m.state for m in mailbox.load(tmp_path, "boss")] == ["new", "done"]


class Item:
    def __init__(self, id, text, target="boss", source="xr1"):
        self.id, self.text, self.target, self.source = id, text, target, source
        self.created, self.human = 1_800_000_000.0, False


@pytest.fixture
def readers(state_dir):
    (state_dir / "config.toml").write_text('[mailbox]\nreaders = ["boss"]\n', encoding="utf-8")
    return state_dir


def hook(monkeypatch, capsys, event, source=None, name="boss", prompt=None):
    payload = {"hook_event_name": event} | ({"source": source} if source else {})
    payload |= {"prompt": prompt} if prompt else {}
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


def test_session_start_shows_a_new_message_and_leaves_it_new(readers, monkeypatch, capsys):
    mailbox.add(readers, Item("c3c3c3c3c3", "filed while compacting"))
    shown = hook(monkeypatch, capsys, "SessionStart", "compact")
    assert "filed while compacting" in shown["additionalContext"]
    assert [m.state for m in mailbox.load(readers, "boss")] == ["new"]

    line = "[courier] c3c3c3c3c3 from xr1: in your mailbox, courier inbox --show c3c3c3c3c3"
    pointer = hook(monkeypatch, capsys, "UserPromptSubmit", prompt=line)
    assert "filed while compacting" in pointer["additionalContext"]
    assert [m.state for m in mailbox.load(readers, "boss")] == ["read"]


def test_compaction_leaves_out_read_copies_older_than_a_day(readers, monkeypatch, capsys):
    old, young = Item("old1", "last week"), Item("young1", "this morning")
    old.created = time.time() - mailbox.AGAIN_SECONDS - 60
    young.created = time.time() - 3600
    for item in (old, young):
        mailbox.add(readers, item)
    hook(monkeypatch, capsys, "UserPromptSubmit")
    again = hook(monkeypatch, capsys, "SessionStart", "compact")["additionalContext"]
    assert "this morning" in again and "last week" not in again
    assert [m.state for m in mailbox.load(readers, "boss")] == ["read", "read"]


def test_a_capped_show_keeps_the_pointed_message_and_the_newest(readers, monkeypatch, capsys):
    now = time.time()
    pointed = Item("a0a0a0a0a0", "the pointed one")
    pointed.created = now - 7200
    mailbox.add(readers, pointed)
    for n in range(mailbox.HOOK_MESSAGES + 5):
        item = Item(f"n{n:02d}", f"filler {n:02d}")
        item.created = now - 3600 + n
        mailbox.add(readers, item)
    late = Item("late1", "filed late with an early time")
    late.created = now - 3000
    mailbox.add(readers, late)
    line = "[courier] a0a0a0a0a0 from xr1: in your mailbox, courier inbox --show a0a0a0a0a0"
    text = hook(monkeypatch, capsys, "UserPromptSubmit", prompt=line)["additionalContext"]
    assert "the pointed one" in text and "filed late with an early time" in text
    assert "filler 14" in text and "filler 05" not in text
    shown = [line.split()[1] for line in text.splitlines() if line.startswith("--- ")]
    assert len(shown) == mailbox.HOOK_MESSAGES and shown[0] == "a0a0a0a0a0"
    assert "filler 00" not in text


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


def submitted(pane, count):
    deadline = time.monotonic() + 5
    while len(pane.submitted) < count and time.monotonic() < deadline:
        time.sleep(0.05)
    return pane.submitted


def test_a_reader_gets_one_pointer_line_and_the_hook_brings_the_text_once(
    herdr, start, tmp_path, monkeypatch, capsys
):
    (tmp_path / "config.toml").write_text('[mailbox]\nreaders = ["boss"]\n', encoding="utf-8")
    pane = herdr.add("w1:p1", "boss")
    server, service = start(herdr, mailbox=True, pointer_for=("boss",))
    item = send(server, service, "merge-ready RL#24")
    line = f"[courier] {item['id']} from xr1: in your mailbox, courier inbox --show {item['id']}"
    assert submitted(pane, 1) == [line]

    prompt = {"prompt": line}
    first = hook(monkeypatch, capsys, "UserPromptSubmit", **prompt)["additionalContext"]
    assert "merge-ready RL#24" in first and "came again" not in first
    again = hook(monkeypatch, capsys, "UserPromptSubmit", **prompt)["additionalContext"]
    assert "merge-ready RL#24" not in again
    assert f"{item['id']} came again and was shown before (read)" in again


def test_pointer_lines_merge_into_one_prompt_and_others_keep_their_text(herdr, start, tmp_path):
    boss = herdr.add("w1:p1", "boss")
    other = herdr.add("w1:p2", "xr2")
    server, service = start(herdr, mailbox=True, pointer_for=("boss",))
    service.dispatcher.hold("boss", owner="t", reason="held", ttl=None)
    first = send(server, service, "first")
    second = send(server, service, "second")
    command = send(server, service, "/compact")
    plain = send(server, service, "for xr2", target="xr2")
    service.dispatcher.release("boss")
    lines = [
        f"[courier] {i} from xr1: in your mailbox, courier inbox --show {i}"
        for i in (first["id"], second["id"])
    ]
    assert submitted(boss, 2) == ["\n".join(lines), "/compact"]
    assert submitted(other, 1) == ["for xr2"]
    assert command["id"] and plain["id"]


def test_hook_names_a_pointed_message_not_yet_filed(readers, monkeypatch, capsys):
    monkeypatch.setattr(mailbox, "FILED_WAIT", 0.2)
    mailbox.add(readers, Item("a1", "first"))
    prompt = "[courier] 0123456789 from xr1: in your mailbox, courier inbox --show 0123456789"
    shown = hook(monkeypatch, capsys, "UserPromptSubmit", prompt=prompt)["additionalContext"]
    assert "first" in shown
    assert "0123456789 is not in the mailbox yet" in shown


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


def test_showing_a_message_by_id_marks_it_shown(state_dir, capsys):
    mailbox.add(state_dir, Item("a1", "first"))
    mailbox.add(state_dir, Item("b2", "second"))
    assert cli.main(["inbox", "boss", "--show", "a1"]) == 0
    assert "first" in capsys.readouterr().out
    assert {m.id: m.state for m in mailbox.load(state_dir, "boss")} == {"a1": "read", "b2": "new"}
    assert cli.main(["inbox", "boss"]) == 0
    assert {m.id: m.state for m in mailbox.load(state_dir, "boss")}["b2"] == "new"


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


def wait_for(check, seconds=5):
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)
    return check()


def test_a_reader_with_text_in_its_box_gets_mail_from_the_hook_and_nothing_is_typed(
    herdr, start, tmp_path, monkeypatch, capsys
):
    (tmp_path / "config.toml").write_text('[mailbox]\nreaders = ["boss"]\n', encoding="utf-8")
    boss = herdr.add("w1:p1", "boss", box="half a sentence the owner is typing")
    server, service = start(herdr, mailbox=True, pointer_for=("boss",))
    items = [send(server, service, f"message {n}") for n in range(3)]
    d = service.dispatcher
    assert wait_for(lambda: all(d.items[i["id"]].state == "filed" for i in items))
    assert boss.submit_calls == 0
    assert [i["id"] for i in d.status()["filed"]] == [i["id"] for i in items]
    # The owner sends what they typed: the prompt-submit hook brings all three.
    shown = hook(monkeypatch, capsys, "UserPromptSubmit", prompt="half a sentence")
    assert all(f"message {n}" in shown["additionalContext"] for n in range(3))
    monkeypatch.setattr("herdr_courier.core.queue.MAILBOX_EVERY", 0.0)
    assert wait_for(lambda: all(d.items[i["id"]].state == "sent" for i in items))
    assert d.items[items[0]["id"]].reason == "shown to boss by its hook"


def test_the_stop_hook_hands_a_working_reader_its_new_mail_once(
    herdr, start, tmp_path, monkeypatch, capsys
):
    (tmp_path / "config.toml").write_text('[mailbox]\nreaders = ["boss"]\n', encoding="utf-8")
    boss = herdr.add("w1:p1", "boss", status="working")
    server, service = start(herdr, mailbox=True, pointer_for=("boss",))
    item = send(server, service, "land AMG#73")
    d = service.dispatcher
    assert wait_for(lambda: d.items[item["id"]].state == "filed")
    assert boss.submit_calls == 0

    def stop(active):
        payload = {"hook_event_name": "Stop", "stop_hook_active": active}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert cli.main(["inbox", "boss", "--hook"]) == 0
        return capsys.readouterr().out

    first = json.loads(stop(False))
    assert first["decision"] == "block" and "land AMG#73" in first["reason"]
    assert stop(True) == ""


def test_a_pointer_left_in_the_box_keeps_its_message_filed_until_shown(
    herdr, start, tmp_path, monkeypatch, capsys
):
    (tmp_path / "config.toml").write_text('[mailbox]\nreaders = ["boss"]\n', encoding="utf-8")
    boss = herdr.add("w1:p1", "boss", submit_mode="stuck")
    server, service = start(herdr, mailbox=True, pointer_for=("boss",))
    item = send(server, service, "merge-ready RL#24")
    d = service.dispatcher
    assert wait_for(lambda: d.items[item["id"]].state == "filed", seconds=30)
    assert d.items[item["id"]].reason.startswith("pointer ghost")
    assert boss.box.startswith("[courier]")
    hook(monkeypatch, capsys, "UserPromptSubmit", prompt="something else")
    monkeypatch.setattr("herdr_courier.core.queue.MAILBOX_EVERY", 0.0)
    assert wait_for(lambda: d.items[item["id"]].state == "sent")
