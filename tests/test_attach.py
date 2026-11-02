import hashlib
import json
import re

import pytest
from conftest import call

from herdr_courier import attach, cli, mailbox


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.png").write_bytes(b"\x89PNG" + b"0" * 2000)
    (src / "notes.txt").write_text("sample notes", encoding="utf-8")
    return src


def test_files_land_in_one_folder_named_after_the_message(files, tmp_path):
    inbox = tmp_path / "inbox"
    folder, done = attach.stage(
        [str(files / "a.png"), str(files / "notes.txt")], inbox, "0123456789", now=1_800_000_000
    )
    assert folder.parent == inbox
    assert re.fullmatch(r"\d{8}-\d{6}-0123456789", folder.name)
    assert sorted(p.name for p in folder.iterdir()) == ["a.png", "notes.txt"]
    png = done[0]
    assert png.path == str(folder / "a.png")
    assert png.from_path == str((files / "a.png").resolve())
    assert (png.size, png.sha256) == (
        2004,
        hashlib.sha256((files / "a.png").read_bytes()).hexdigest(),
    )
    assert attach.line(png) == (
        f"file: {folder / 'a.png'} (from {png.from_machine} {png.from_path}, 2 KB, "
        f"sha256 {png.sha256[:12]})"
    )


def test_a_machine_prefix_fetches_and_a_drive_letter_does_not():
    assert attach.split("pc-b:C:/Users/me/a.png") == ("pc-b", "C:/Users/me/a.png")
    assert attach.split("C:/Users/me/a.png") == (None, "C:/Users/me/a.png")
    assert attach.split("/tmp/a.png") == (None, "/tmp/a.png")


def test_remote_files_keep_their_origin(tmp_path):
    fetched = []

    def fetch(machine, path, folder, name):
        fetched.append((machine, path))
        (folder / name).write_bytes(b"x" * 10)

    _, done = attach.stage(
        [r"pc-b:C:\Users\me\shot.png"], tmp_path / "inbox", "0123456789", fetch=fetch
    )
    assert fetched == [("pc-b", r"C:\Users\me\shot.png")]
    assert (done[0].from_machine, done[0].from_path, done[0].size) == (
        "pc-b",
        r"C:\Users\me\shot.png",
        10,
    )
    assert done[0].path.endswith("shot.png")


def test_one_failed_copy_leaves_nothing_behind(files, tmp_path):
    inbox = tmp_path / "inbox"

    def fetch(machine, path, folder, name):
        raise attach.AttachError(f"scp from {machine} failed: No such file")

    with pytest.raises(attach.AttachError, match="scp from pc-b failed"):
        attach.stage([str(files / "a.png"), "pc-b:C:/gone.png"], inbox, "0123456789", fetch=fetch)
    assert list(inbox.iterdir()) == []


@pytest.mark.parametrize(
    ("names", "limits", "expect"),
    [
        (["a.png", "missing.png"], {}, "no such file"),
        (["a.png"] * 2, {}, "two files named a.png"),
        (["a.png"] * 11, {}, "at most 10"),
        (["a.png"], {"MAX_FILE": 1000}, "per file"),
        (["a.png", "notes.txt"], {"MAX_TOTAL": 2010}, "per message"),
    ],
)
def test_limits_are_checked_before_anything_is_copied(
    files, tmp_path, monkeypatch, names, limits, expect
):
    for key, value in limits.items():
        monkeypatch.setattr(attach, key, value)
    inbox = tmp_path / "inbox"
    with pytest.raises(attach.AttachError, match=expect):
        attach.stage([str(files / n) for n in names], inbox, "0123456789")
    assert not inbox.exists()


def test_a_remote_file_over_the_limit_is_removed_after_the_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(attach, "MAX_FILE", 5)
    inbox = tmp_path / "inbox"

    def fetch(machine, path, folder, name):
        (folder / name).write_bytes(b"x" * 10)

    with pytest.raises(attach.AttachError, match="per file"):
        attach.stage(["pc-b:/big.bin"], inbox, "0123456789", fetch=fetch)
    assert list(inbox.iterdir()) == []


def test_inbox_setting_expands_and_must_be_absolute(monkeypatch, tmp_path, state_dir):
    monkeypatch.setenv("COURIER_TEST_INBOX", str(tmp_path))
    assert attach.inbox_dir("") == state_dir / "inbox"
    assert attach.inbox_dir("$COURIER_TEST_INBOX/inbox") == tmp_path / "inbox"
    with pytest.raises(attach.AttachError, match="absolute"):
        attach.inbox_dir("inbox")


def test_send_file_names_the_copy_and_the_mailbox_keeps_it(herdr, start, files, tmp_path, capsys):
    herdr.add("w1:p1", "boss")
    start(herdr, mailbox=True)
    assert cli.main(["send", "boss", "look at this", "--file", str(files / "a.png")]) == 0
    out = capsys.readouterr().out
    item_id = re.search(r"queued (\w+)", out)[1]
    folder = next((tmp_path / "inbox").iterdir())
    assert folder.name.endswith(f"-{item_id}")
    assert f"1 file(s) in {folder}" in out
    [kept] = mailbox.load(tmp_path, "boss")
    assert kept.id == item_id
    assert kept.text.splitlines()[0] == "look at this"
    assert kept.text.splitlines()[1].startswith(f"file: {folder / 'a.png'} (from ")
    assert kept.attachments[0]["path"] == str(folder / "a.png")
    assert (
        kept.attachments[0]["sha256"] == hashlib.sha256((files / "a.png").read_bytes()).hexdigest()
    )


def test_send_file_that_cannot_be_copied_sends_nothing(herdr, start, files, tmp_path, capsys):
    herdr.add("w1:p1", "boss")
    _, service = start(herdr)
    code = cli.main(
        ["send", "boss", "two files", "--file", str(files / "a.png"), "--file", str(files / "gone")]
    )
    assert code == cli.EXIT_FAILED
    assert "nothing was sent" in capsys.readouterr().err
    assert service.dispatcher.items == {}
    assert not (tmp_path / "inbox").exists()


def test_refused_send_removes_the_copies(herdr, start, files, tmp_path, capsys):
    start(herdr)
    with pytest.raises(SystemExit) as exc:
        cli.main(["send", "nobody", "hi", "--file", str(files / "a.png")])
    assert exc.value.code == cli.EXIT_FAILED
    assert list((tmp_path / "inbox").iterdir()) == []


def test_serve_checks_the_id_and_attachments(herdr, start):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    good = {"path": "/x/a", "from_machine": "m", "from_path": "/a", "size": 1, "sha256": "0" * 64}
    base = {"target": "boss", "text": "t"}
    for body, detail in [
        (base | {"id": "XYZ"}, "id must be"),
        (base | {"attachments": [good | {"size": -1}]}, "size"),
        (base | {"attachments": [good | {"sha256": "abc"}]}, "sha256"),
        (base | {"attachments": [good | {"extra": 1}]}, "exactly"),
        (base | {"attachments": [good] * 11}, "at most 10"),
    ]:
        status, reply = call(server, "POST", "/api/dispatch/send", body, service.token)
        assert (status, detail in reply["detail"]) == (400, True), json.dumps(body)
    status, reply = call(
        server, "POST", "/api/dispatch/send", base | {"id": "0123456789"}, service.token
    )
    assert (status, reply["item"]["id"], reply["attachments"]) == (200, "0123456789", 0)
    status, reply = call(
        server, "POST", "/api/dispatch/send", base | {"id": "0123456789"}, service.token
    )
    assert status == 400 and "in use" in reply["detail"]
