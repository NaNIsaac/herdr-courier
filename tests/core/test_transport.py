import os
import shutil

import pytest

from herdr_courier.core.transport import CliHerdr, HerdrError, PaneHerdr, same_socket

needs_herdr = pytest.mark.skipif(
    shutil.which(os.environ.get("HERDR_BIN_PATH") or "herdr") is None, reason="needs the herdr CLI"
)


@needs_herdr
def test_server_socket_is_the_one_the_environment_names(tmp_path, monkeypatch):
    # No server listens there: herdr still names the socket and reaches nothing else.
    socket = str(tmp_path / "herdr.sock")
    monkeypatch.setenv("HERDR_SOCKET_PATH", socket)
    herdr = CliHerdr()
    assert same_socket(herdr.server_socket(), socket)
    with pytest.raises(HerdrError) as missing:
        herdr.agent_get("boss")
    assert missing.value.code == "server_not_running"


def test_same_socket_ignores_spelling_only():
    assert same_socket(os.path.join("a", "herdr.sock"), os.path.join("a", ".", "herdr.sock"))
    assert not same_socket(os.path.join("a", "herdr.sock"), os.path.join("b", "herdr.sock"))


@pytest.mark.parametrize("transport", [CliHerdr, PaneHerdr])
def test_recovery_enter_sends_only_one_key(transport, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "herdr_courier.core.transport._run", lambda args, timeout: calls.append(args)
    )
    transport().enter("w1:p1")
    assert calls == [["pane", "send-keys", "w1:p1", "enter"]]
