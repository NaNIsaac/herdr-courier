import json
import os

from herdr_courier import ledger


def entry(state, attempts=0):
    task = ledger.new("BL-1", "boss", "w1", ["file missing.txt"])
    task.update(state=state, attempts=attempts)
    return json.dumps({"event": "task", "task": task}) + "\n"


def test_history_uses_utc_stamp_not_restored_mtime(tmp_path):
    older = tmp_path / "journal.20261029T010000Z.jsonl"
    newer = tmp_path / "journal.20261029T020000Z.jsonl"
    older.write_text(entry("OPEN"))
    newer.write_text(entry("DONE", 2))
    os.utime(older, (100, 100))
    os.utime(newer, (1, 1))
    tasks = ledger.read_history(tmp_path / "journal.jsonl")
    assert tasks["BL-1"]["state"] == "DONE"
    assert tasks["BL-1"]["attempts"] == 2


def test_same_second_suffix_is_numeric(tmp_path):
    for suffix in (0, 2, 9, 10):
        tail = "" if suffix == 0 else f"-{suffix}"
        path = tmp_path / f"journal.20261029T010000Z{tail}.jsonl"
        path.write_text(entry("DONE", suffix))
        os.utime(path, (100 - suffix, 100 - suffix))
    assert ledger.read_history(tmp_path / "journal.jsonl")["BL-1"]["attempts"] == 10


def test_pruning_keeps_newest_utc_generations(make_dispatcher, monkeypatch):
    d = make_dispatcher()
    for second in range(14):
        path = d.journal.with_name(f"journal.20261029T0100{second:02}Z.jsonl")
        path.write_text("\n")
        os.utime(path, (100 - second, 100 - second))
    d.journal.write_text("\n")
    monkeypatch.setattr(ledger.time, "strftime", lambda *args: "20261029T020000Z")
    d._close_generation()
    names = sorted(path.name for path in d.journal.parent.glob("journal.[0-9]*.jsonl"))
    assert len(names) == 10
    assert names[0] == "journal.20261029T010005Z.jsonl"
    assert names[-1] == "journal.20261029T020000Z.jsonl"


def test_rotation_does_not_reuse_a_pruned_same_second_suffix(make_dispatcher, monkeypatch):
    d = make_dispatcher()
    for suffix in (9, 10):
        d.journal.with_name(f"journal.20261029T010000Z-{suffix}.jsonl").write_text("\n")
    d.journal.write_text("\n")
    monkeypatch.setattr(ledger.time, "strftime", lambda *args: "20261029T010000Z")
    d._close_generation()
    assert d.journal.with_name("journal.20261029T010000Z-11.jsonl").exists()
    assert not d.journal.with_name("journal.20261029T010000Z.jsonl").exists()
