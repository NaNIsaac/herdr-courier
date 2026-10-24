import http.client
import json
import os
import threading
import time
import tomllib
from pathlib import Path

import pytest

from herdr_courier import cli, daemon
from herdr_courier.core.queue import Dispatcher
from herdr_courier.daemon import AlreadyRunning, DispatchService, InstanceLock, Server
from herdr_courier.state import endpoint_path, lock_path


@pytest.fixture
def start(tmp_path, monkeypatch):
    """Start the dispatcher on a free port with a fake herdr; courier finds it through COURIER_STATE_DIR."""
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("herdr_courier.core.gate.DEBOUNCE", 0.1)
    running = []

    def go(herdr, *, enabled=True, tick=0.05):
        dispatcher = (
            Dispatcher(herdr, tmp_path / "journal.jsonl", log=lambda _: None) if enabled else None
        )
        service = DispatchService(dispatcher, tmp_path, tick=tick)
        server = Server(service)
        service.start(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        running.append((server, service, thread))
        return server, service

    yield go
    for server, service, thread in running:
        server.shutdown()
        server.server_close()
        service.stop()
        thread.join(timeout=5)


def call(server, method, path, body=None, token=None, host="127.0.0.1", raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers = {"Host": host, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (None if body is None else json.dumps(body))
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, payload


def test_routes_need_the_token(herdr, start):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    body = {"target": "boss", "text": "hello"}
    assert call(server, "POST", "/api/dispatch/send", body)[0] == 401
    assert call(server, "POST", "/api/dispatch/send", body, token="nope")[0] == 401
    assert call(server, "POST", "/api/dispatch/send", body, token=service.token)[0] == 200


def test_send_hold_release_cancel_and_status(herdr, start):
    herdr.add("w1:p1", "boss", box="isaac typing")
    server, service = start(herdr)
    t = service.token
    _, sent = call(server, "POST", "/api/dispatch/send", {"target": "boss", "text": "r"}, t)
    item = sent["item"]
    assert item["pane_id"] == "w1:p1"
    status, held = call(server, "POST", "/api/dispatch/hold", {"target": "boss", "ttl": 60}, t)
    assert (status, held["pane_id"], held["api"]) == (200, "w1:p1", 1)
    _, status = call(server, "GET", "/api/dispatch/status", token=t)
    assert status["status"]["panes"]["w1:p1"]["hold"]["seconds_left"] == 60
    assert status["worker"]["alive"] is True
    assert call(server, "POST", "/api/dispatch/release", {"target": "boss"}, t)[0] == 200
    _, cancelled = call(server, "POST", "/api/dispatch/cancel", {"id": item["id"]}, t)
    assert cancelled["item"]["state"] == "cancelled"


def test_unknown_target_is_a_400(herdr, start):
    server, service = start(herdr)
    status, body = call(
        server, "POST", "/api/dispatch/send", {"target": "nobody", "text": "x"}, service.token
    )
    assert status == 400
    assert "cannot resolve" in body["detail"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"text": "x"}, "target"),
        ({"target": "boss", "text": ""}, "text"),
        ({"target": "boss", "text": "x" * 20_001}, "text"),
        ({"target": "boss", "text": "x", "human": "yes"}, "human"),
        ({"target": "boss", "text": "x", "client_id": "short"}, "client_id"),
    ],
)
def test_send_body_is_validated(herdr, start, body, detail):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    status, reply = call(server, "POST", "/api/dispatch/send", body, service.token)
    assert status == 400
    assert detail in reply["detail"]


def test_hold_ttl_must_be_positive(herdr, start):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    status, _ = call(
        server, "POST", "/api/dispatch/hold", {"target": "boss", "ttl": 0}, service.token
    )
    assert status == 400


def test_foreign_host_header_is_refused(herdr, start):
    server, service = start(herdr)
    status, _ = call(
        server, "GET", "/api/dispatch/status", token=service.token, host="evil.example"
    )
    assert status == 400


def test_body_limits_and_json(herdr, start):
    server, service = start(herdr)
    t = service.token
    assert call(server, "POST", "/api/dispatch/send", token=t, raw="x" * (64 * 1024 + 1))[0] == 413
    assert call(server, "POST", "/api/dispatch/send", token=t, raw="{not json")[0] == 400
    assert call(server, "POST", "/api/dispatch/send", token=t, raw="[1, 2]")[0] == 400
    assert call(server, "GET", "/elsewhere", token=t)[0] == 404


def test_dispatch_off_answers_503(herdr, start):
    server, service = start(herdr, enabled=False)
    assert call(server, "GET", "/api/dispatch/status", token=service.token)[0] == 503


def test_send_with_same_client_id_returns_the_same_item(herdr, start):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    body = {"target": "boss", "text": "hi", "client_id": "abcdef0123456789"}
    _, first = call(server, "POST", "/api/dispatch/send", body, service.token)
    _, again = call(server, "POST", "/api/dispatch/send", body, service.token)
    assert first["item"]["id"] == again["item"]["id"]


def test_second_instance_is_refused(tmp_path, monkeypatch):
    first = InstanceLock(lock_path(tmp_path))
    with pytest.raises(AlreadyRunning):
        InstanceLock(lock_path(tmp_path))
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    assert cli.main(["serve", "--port", "0"]) == 1
    first.release()
    InstanceLock(lock_path(tmp_path)).release()


def test_stop_leaves_another_instances_endpoint_alone(tmp_path, herdr):
    running = DispatchService(Dispatcher(herdr, tmp_path / "j.jsonl", log=lambda _: None), tmp_path)
    running.start(8765)
    other = DispatchService(Dispatcher(herdr, tmp_path / "k.jsonl", log=lambda _: None), tmp_path)
    other.stop()
    assert json.loads(endpoint_path(tmp_path).read_text(encoding="utf-8"))["token"] == running.token
    running.stop()
    assert not endpoint_path(tmp_path).exists()


def test_worker_survives_a_failing_tick_and_reports_it(herdr, start):
    server, service = start(herdr, tick=0.01)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("journal unwritable")

    service.dispatcher.tick = flaky
    deadline = time.monotonic() + 5
    while len(calls) < 5:
        assert time.monotonic() < deadline, "worker stopped after the failing tick"
        time.sleep(0.01)
    _, status = call(server, "GET", "/api/dispatch/status", token=service.token)
    assert status["worker"]["alive"] is True
    assert status["worker"]["errors"] == 1
    assert "journal unwritable" in status["worker"]["last_error"]


def test_endpoint_file_is_written_over_a_stale_temp_file(tmp_path, herdr):
    path = endpoint_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix(".tmp").write_text("stale", encoding="utf-8")
    service = DispatchService(Dispatcher(herdr, tmp_path / "j.jsonl", log=lambda _: None), tmp_path)
    service.start(8765)
    try:
        assert json.loads(path.read_text(encoding="utf-8"))["token"] == service.token
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        service.stop()


def test_send_wait_through_live_dispatcher(herdr, start, capsys, monkeypatch):
    pane = herdr.add("w1:p1", "boss")
    start(herdr)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0}))
    assert cli.main(["send", "boss", "[lane] done", "--wait", "--timeout", "10000"]) == 0
    assert pane.submitted == ["[lane] done"]
    assert "sent" in capsys.readouterr().out


def test_hc_status_shows_why_a_message_waits(herdr, start, capsys):
    herdr.add("w1:p1", "boss", box="isaac typing")
    start(herdr)
    assert cli.main(["send", "boss", "[lane] report"]) == 0
    time.sleep(0.3)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "input typed" in out
    assert "observed: idle, typed" in out


def test_hc_status_by_name_shows_a_hold_with_nothing_queued(herdr, start, capsys):
    herdr.add("w1:p1", "boss")
    start(herdr)
    assert cli.main(["hold", "boss", "--reason", "phone"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "boss"]) == 0
    assert "w1:p1  HOLD phone" in capsys.readouterr().out


def test_hc_retries_a_lost_reply_without_queuing_twice(herdr, start, monkeypatch):
    pane = herdr.add("w1:p1", "boss")
    _, service = start(herdr)
    real = cli.request
    calls = []

    def lossy(method, path, body=None, timeout=10.0):
        reply = real(method, path, body, timeout)
        calls.append(path)
        if path == "send" and len(calls) == 1:
            raise cli.LostContact("reply lost")
        return reply

    monkeypatch.setattr(cli, "request", lossy)
    assert cli.main(["send", "boss", "[lane] once"]) == 0
    assert calls == ["send", "send"]
    time.sleep(0.5)
    assert len(service.dispatcher.items) == 1
    assert pane.submitted == ["[lane] once"]


def test_hc_without_daemon_fails_loudly_with_manual_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        cli.main(["send", "boss", "[lane] report"])
    assert exc.value.code == cli.EXIT_UNAVAILABLE
    err = capsys.readouterr().err
    assert "Nothing was sent" in err
    assert "Start courier serve" in err
    assert 'herdr agent prompt boss "[lane] report"' in err


def test_hc_with_stale_endpoint_counts_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    path = endpoint_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": 9, "token": "t", "pid": 1}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["status"])
    assert exc.value.code == cli.EXIT_UNAVAILABLE


def raw_request(server, payload: bytes) -> bytes:
    """Send bytes on one connection and return everything the server writes before closing."""
    import socket

    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(payload)
        chunks = []
        while data := sock.recv(65536):
            chunks.append(data)
    return b"".join(chunks)


@pytest.mark.parametrize("ttl", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_ttl_is_refused_and_status_keeps_working(herdr, start, ttl):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    raw = f'{{"target": "boss", "ttl": {ttl}}}'
    status, _ = call(server, "POST", "/api/dispatch/hold", token=service.token, raw=raw)
    assert status == 400
    assert call(server, "GET", "/api/dispatch/status", token=service.token)[0] == 200
    assert service.dispatcher.holds == {}


def test_ttl_has_an_upper_bound(herdr, start):
    herdr.add("w1:p1", "boss")
    server, service = start(herdr)
    body = {"target": "boss", "ttl": daemon.MAX_HOLD_SECONDS + 1}
    assert call(server, "POST", "/api/dispatch/hold", body, service.token)[0] == 400


def test_non_finite_wait_timeout_is_refused(herdr, start):
    server, service = start(herdr)
    raw = '{"id": "abc", "timeout": NaN}'
    assert call(server, "POST", "/api/dispatch/wait", token=service.token, raw=raw)[0] == 400


def test_chunked_bodies_are_refused_and_the_connection_closed(herdr, start):
    server, service = start(herdr)
    reply = raw_request(
        server,
        b"POST /api/dispatch/send HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer " + service.token.encode() + b"\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n0\r\n\r\n"
        b"GET /api/dispatch/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
    )
    assert reply.startswith(b"HTTP/1.1 411")
    assert reply.count(b"HTTP/1.1 ") == 1


@pytest.mark.parametrize("length", ["abc", "-1", "1.5"])
def test_bad_content_length_is_a_400(herdr, start, length):
    server, service = start(herdr)
    reply = raw_request(
        server,
        b"POST /api/dispatch/send HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer " + service.token.encode() + b"\r\n"
        b"Content-Length: " + length.encode() + b"\r\n\r\n{}",
    )
    assert reply.startswith(b"HTTP/1.1 400")


def test_body_after_an_early_401_is_not_parsed_as_a_request(herdr, start):
    server, _ = start(herdr)
    smuggled = b"GET /api/dispatch/status HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    reply = raw_request(
        server,
        b"POST /api/dispatch/send HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled,
    )
    assert reply.startswith(b"HTTP/1.1 401")
    assert reply.count(b"HTTP/1.1 ") == 1


def test_non_ascii_authorization_is_a_401(herdr, start):
    server, _ = start(herdr)
    reply = raw_request(
        server,
        b"GET /api/dispatch/status HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Authorization: Bearer \xc3\xa9\xff\r\n\r\n",
    )
    assert reply.startswith(b"HTTP/1.1 401")


def test_client_refuses_an_unknown_api_version(herdr, start, monkeypatch):
    start(herdr)
    monkeypatch.setattr("herdr_courier.client.API_VERSION", 2)
    with pytest.raises(SystemExit) as exc:
        cli.main(["status"])
    assert exc.value.code == cli.EXIT_UNAVAILABLE


def test_malformed_endpoint_file_counts_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    endpoint_path(tmp_path).write_text('{"token": "t"}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.main(["status"])
    assert exc.value.code == cli.EXIT_UNAVAILABLE


def test_endpoint_pointing_at_another_server_counts_as_unavailable(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Html(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"<html>")

        def log_message(self, *args):
            pass

    other = HTTPServer(("127.0.0.1", 0), Html)
    thread = threading.Thread(target=other.handle_request)
    thread.start()
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    endpoint_path(tmp_path).write_text(
        json.dumps({"port": other.server_address[1], "token": "t", "pid": 1}), encoding="utf-8"
    )
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main(["status"])
        assert exc.value.code == cli.EXIT_UNAVAILABLE
    finally:
        thread.join(timeout=5)
        other.server_close()


def help_text(run, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_serve_is_a_subcommand_of_courier_and_hc_and_hcd_is_its_alias(capsys):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))
    assert project["project"]["scripts"] == {
        "courier": "herdr_courier.cli:main",
        "hc": "herdr_courier.cli:main",
        "hcd": "herdr_courier.cli:serve_main",
    }
    out = help_text(lambda: cli.main(["serve", "--help"]), capsys)
    assert out.startswith("usage: courier serve")
    assert "--port" in out and "--transport" in out
    alias = help_text(lambda: cli.serve_main(["--help"]), capsys)
    assert alias.startswith("usage: hcd")
    assert alias.split("\n\n", 1)[1] == out.split("\n\n", 1)[1]


def test_hcd_is_refused_like_courier_serve_while_one_runs(tmp_path, monkeypatch):
    first = InstanceLock(lock_path(tmp_path))
    monkeypatch.setenv("COURIER_STATE_DIR", str(tmp_path))
    assert cli.serve_main(["--port", "0"]) == 1
    first.release()
