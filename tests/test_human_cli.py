import json
import sys

import pytest

from herdr_courier import config, display
from herdr_courier import human_cli as H
from herdr_courier import questions as Q

PROVIDER = r"""
import json, pathlib, sys, time
path, mode, operation = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
request = json.load(sys.stdin)
if mode == "timeout":
    time.sleep(3)
if mode == "malformed":
    print("not json")
    sys.exit()
if mode == "invalid_utf8":
    sys.stdout.buffer.write(b"\xff")
    sys.exit()
if mode == "shape":
    print("[]")
    sys.exit()
if mode == "nonfinite":
    print('{"items": [], "extra": NaN}')
    sys.exit()
if mode == "oversized":
    print("x" * (1024 * 1024 + 100))
    sys.exit()
if mode == "stderr":
    sys.stderr.write("x" * (64 * 1024 + 100))
    sys.exit()
if mode == "unavailable":
    sys.stderr.write("source unavailable")
    sys.exit(2)
state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
state.setdefault("calls", []).append({"operation": operation, "request": request})
item = {
    "id": "a:17", "from": "reviewer", "to": "alex",
    "text": "첫 질문입니다.\n다음 줄도 온전히 보여 주세요.",
    "created": 1700000000, "state": state.get("state", "open"),
    "revision": str(state.get("revision", 1)),
    "options": ["진행", "보류"], "links": ["https://example.com/item"],
}
if mode == "foreign":
    item["to"] = "someone-else"
if operation == "list":
    output = {"items": [item, item] if mode == "duplicate" else [item]}
elif operation == "get":
    if mode == "changed":
        state["revision"] = state.get("revision", 1) + 1
        item["revision"] = str(state["revision"])
    output = item
else:
    if request["revision"] != item["revision"]:
        sys.stderr.write("revision conflict")
        sys.exit(1)
    state["state"] = "answered"
    state["revision"] = state.get("revision", 1) + 1
    state["answer"] = request["text"]
    output = {"state": "answered", "receipt": request["request_id"], "notification": "handled"}
path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
if mode == "answer_timeout" and operation == "answer":
    time.sleep(3)
print(json.dumps(output, ensure_ascii=False))
"""


@pytest.fixture
def providers(tmp_path, state_dir):
    script = tmp_path / "provider.py"
    script.write_text(PROVIDER, encoding="utf-8")
    paths = {}

    def setup(**modes):
        parts = ['[human]\nname = "alex"\ndefault_to = "writer"\n']
        for name, mode in modes.items():
            paths[name] = tmp_path / f"{name}.json"
            command = [sys.executable, str(script), str(paths[name]), mode]
            parts.append(
                f"[questions.adapters.{name}]\ncommand = {json.dumps(command)}\ntimeout = 0.5\n"
            )
        config.config_path().write_text("\n".join(parts), encoding="utf-8")
        return paths

    return setup


def test_no_provider_is_not_an_empty_queue(capsys):
    assert H.main(["q"]) == 3
    assert "No question providers configured" in capsys.readouterr().out
    assert H.main(["q", "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["configured"] is False


def test_list_get_korean_and_namespaced_ids(providers, capsys):
    providers(first="good", second="good")
    assert H.main(["q", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in listing["items"]} == {"first:a:17", "second:a:17"}
    assert H.main(["q", "first:a:17", "--width", "14"]) == 0
    output = capsys.readouterr().out
    assert all(display.cols(line) <= 14 for line in output.splitlines())
    assert "다음줄도온전히보여주세요." in "".join(output.split())
    assert H.main(["q", "first:a:17", "--json"]) == 0
    assert "첫 질문" in json.loads(capsys.readouterr().out)["item"]["text"]


def test_provider_exchange_is_utf8_without_caller_settings(providers, monkeypatch, capsys):
    # Windows runners default to the ANSI code page; the provider must still print Korean.
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    env = Q.provider_env()
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"
    monkeypatch.setenv("PYTHONIOENCODING", "utf-16")
    assert Q.provider_env()["PYTHONIOENCODING"] == "utf-16"
    monkeypatch.delenv("PYTHONIOENCODING")
    providers(first="good", second="good")
    assert H.main(["q", "first:a:17", "--json"]) == 0
    assert "첫 질문" in json.loads(capsys.readouterr().out)["item"]["text"]


def test_answer_file_alias_and_repeat_does_not_answer_twice(providers, tmp_path, capsys):
    paths = providers(first="good")
    answer = tmp_path / "answer.txt"
    text = '보류합니다.\n따옴표 " ; $(echo unsafe) 를 그대로 보냅니다.'
    answer.write_text(text, encoding="utf-8")
    assert H.main(["answer", "first:a:17", "--file", str(answer), "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "answered" and receipt["receipt"]
    state = json.loads(paths["first"].read_text(encoding="utf-8"))
    assert state["answer"] == text
    calls = state["calls"]
    assert [call["operation"] for call in calls] == ["get", "get", "answer"]
    assert calls[-1]["request"]["revision"] == "1"
    assert calls[-1]["request"]["principal"] == "alex"
    assert H.main(["a", "first:a:17", text]) == 1
    capsys.readouterr()
    state = json.loads(paths["first"].read_text(encoding="utf-8"))
    assert sum(call["operation"] == "answer" for call in state["calls"]) == 1
    assert H.main(["q", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
    assert H.main(["q", "--all", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1


@pytest.mark.parametrize("mode", ["changed", "foreign"])
def test_changed_revision_or_other_person_is_not_answered(providers, capsys, mode):
    paths = providers(first=mode)
    assert H.main(["a", "first:a:17", "진행", "--json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    state = json.loads(paths["first"].read_text(encoding="utf-8"))
    assert not any(call["operation"] == "answer" for call in state["calls"])


@pytest.mark.parametrize(
    "mode",
    [
        "malformed",
        "invalid_utf8",
        "shape",
        "nonfinite",
        "oversized",
        "stderr",
        "timeout",
        "unavailable",
        "duplicate",
    ],
)
def test_provider_errors_are_visible_without_hiding_other_sources(providers, capsys, mode):
    providers(broken=mode, working="good")
    assert H.main(["q", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["errors"][0]["provider"] == "broken"
    assert result["items"][0]["id"] == "working:a:17"


def test_unknown_id_blank_and_missing_file_never_start_a_provider(providers, tmp_path, capsys):
    paths = providers(first="good")
    for args in (
        ["q", "17"],
        ["q", "missing:a:17"],
        ["a", "first:a:17", " "],
        ["a", "first:a:17", "--file", str(tmp_path / "absent")],
    ):
        assert H.main(args) == 1
        capsys.readouterr()
    assert not paths["first"].exists()


def test_config_rejects_shell_string_bad_timeout_and_unknown_keys(state_dir):
    for settings in (
        'command = "echo question"',
        "command = []",
        'command = [""]',
        'command = ["python"]\ntimeout = 0',
        'command = ["python"]\ntimeout = nan',
        'command = ["python"]\nextra = true',
    ):
        (state_dir / "config.toml").write_text("[questions.adapters.example]\n" + settings)
        with pytest.raises(config.ConfigError):
            config.load()


def test_provider_argv_is_not_shell_syntax(providers, monkeypatch):
    providers(first="good; not-a-command")
    provider = config.load().questions.adapters["first"]
    original = Q.subprocess.Popen
    captured = []

    def capture(argv, **kwargs):
        captured.append((argv, kwargs["shell"]))
        return original(argv, **kwargs)

    monkeypatch.setattr(Q.subprocess, "Popen", capture)
    assert Q.listing(provider, "alex")
    assert captured[0][0][-1] == "list"
    assert captured[0][0][-2] == "good; not-a-command"
    assert captured[0][1] is False


def test_answer_timeout_is_not_retried(providers, capsys):
    paths = providers(first="answer_timeout")
    assert H.main(["a", "first:a:17", "진행", "--json"]) == 1
    assert "reconcile" in json.loads(capsys.readouterr().out)["error"]
    assert H.main(["a", "first:a:17", "진행", "--json"]) == 1
    capsys.readouterr()
    state = json.loads(paths["first"].read_text(encoding="utf-8"))
    assert sum(call["operation"] == "answer" for call in state["calls"]) == 1


def test_send_reuses_courier_human_priority_attachments_and_json(
    providers, herdr, start, tmp_path, capsys
):
    herdr.add("p1", "writer")
    _, service = start(herdr)
    providers()
    file = tmp_path / "notes.txt"
    file.write_text("첨부", encoding="utf-8")
    assert H.main(["send", "검토 부탁", "--file", str(file), "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["item"]["human"] is True
    assert receipt["item"]["source"] == "alex"
    assert receipt["attachments"] == 1
    assert "검토 부탁" in receipt["item"]["text"]
    assert service.dispatcher.items[receipt["item"]["id"]].target == "writer"


def test_oversized_request_is_rejected_before_running_provider(providers, monkeypatch):
    providers(first="good")
    provider = config.load().questions.adapters["first"]
    monkeypatch.setattr(Q.subprocess, "Popen", lambda *a, **kw: pytest.fail("provider ran"))
    with pytest.raises(Q.ProviderError, match="request exceeds"):
        Q.invoke(provider, "answer", {"text": "x" * Q.MAX_INPUT})
