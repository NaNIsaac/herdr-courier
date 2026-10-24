import json
import threading
import time

import pytest

from herdr_console import cli
from herdr_console.daemon import remove_endpoint, run_worker, serve, write_endpoint
from herdr_console.dispatcher import Dispatcher


@pytest.fixture
def hcd(herdr, tmp_path, monkeypatch):
    monkeypatch.setenv("HC_STATE_DIR", str(tmp_path))
    dispatcher = Dispatcher(herdr, tmp_path / "journal.jsonl", log=lambda _: None)
    server = serve(dispatcher, "secret")
    write_endpoint({"port": server.server_address[1], "token": "secret", "pid": 1})
    stop = threading.Event()
    threads = [
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}),
        threading.Thread(target=run_worker, args=(dispatcher, stop, 0.05)),
    ]
    for t in threads:
        t.start()
    yield dispatcher
    stop.set()
    server.shutdown()
    server.server_close()
    for t in threads:
        t.join()


def test_send_and_wait_reports_delivery(herdr, hcd, capsys, monkeypatch):
    monkeypatch.setattr("herdr_console.gate.DEBOUNCE", 0.1)
    pane = herdr.add("w1:p1", "boss")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0}))
    code = cli.main(["send", "boss", "[lane] done", "--wait", "--timeout", "10000"])
    assert code == 0
    assert pane.submitted == ["[lane] done"]
    assert "sent" in capsys.readouterr().out


def test_status_shows_waiting_reason(herdr, hcd, capsys):
    herdr.add("w1:p1", "boss", box="isaac typing")
    assert cli.main(["send", "boss", "[lane] report"]) == 0
    time.sleep(0.3)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "w1:p1" in out
    assert "input typed" in out
    assert "observed: idle, typed" in out


def test_hold_and_release_through_cli(herdr, hcd, capsys):
    herdr.add("w1:p1", "boss")
    assert cli.main(["hold", "boss", "--reason", "phone", "--ttl", "60"]) == 0
    assert "holding w1:p1 for 60 s" in capsys.readouterr().out
    assert hcd.holds["w1:p1"].reason == "phone"
    assert cli.main(["release", "boss"]) == 0
    assert "w1:p1" not in hcd.holds


def test_bad_token_is_refused(hcd, tmp_path):
    endpoint = json.loads((tmp_path / "hcd.json").read_text(encoding="utf-8"))
    write_endpoint({**endpoint, "token": "wrong"})
    with pytest.raises(SystemExit) as exc:
        cli.main(["ping"])
    assert exc.value.code == cli.EXIT_FAILED


def test_send_without_hcd_fails_loudly_and_prints_manual_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HC_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cli.main(["send", "boss", "[lane] report"])
    assert exc.value.code == cli.EXIT_UNAVAILABLE
    err = capsys.readouterr().err
    assert "hcd is not running" in err
    assert "Nothing was sent" in err
    assert 'herdr agent prompt boss "[lane] report"' in err


def test_stale_endpoint_counts_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HC_STATE_DIR", str(tmp_path))
    write_endpoint({"port": 9, "token": "t", "pid": 123})
    with pytest.raises(SystemExit) as exc:
        cli.main(["ping"])
    assert exc.value.code == cli.EXIT_UNAVAILABLE
    remove_endpoint(123)
    assert not (tmp_path / "hcd.json").exists()
