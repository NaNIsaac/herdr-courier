import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

from herdr_courier import cli, ledger, verifier
from herdr_courier.client import Refused, request


@pytest.fixture
def workers(herdr):
    herdr.add("boss", "boss")
    herdr.add("w1", "w1")
    return herdr


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "games23"
    template = tmp_path / "empty-template"
    template.mkdir()
    subprocess.run(
        [shutil.which("git"), "init", "--initial-branch=main", f"--template={template}", str(path)],
        env=os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "out").mkdir()
    return path


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    response = tmp_path / "gh-response.json"
    script = bindir / "fake_gh.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] != ['pr', 'view', '5', '--json', 'state,body,mergedAt,url']:\n"
        "    raise SystemExit(2)\n"
        "data = json.loads(Path(os.environ['PILOT_GH_RESPONSE']).read_text())\n"
        "if data.get('error'):\n"
        "    raise SystemExit(1)\n"
        "print(json.dumps(data))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        (bindir / "gh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        command = bindir / "gh"
        command.write_text(f"#!{sys.executable}\n" + script.read_text())
        command.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("PILOT_GH_RESPONSE", str(response))

    def answer(state="OPEN", **fields):
        response.write_text(
            json.dumps(
                {
                    "state": state,
                    "body": "local-ci: abc123 | pytest pass | nothing omitted",
                    "mergedAt": None,
                    "url": "https://github.com/NaNIsaac/games23/pull/5",
                    **fields,
                }
            ),
            encoding="utf-8",
        )

    answer()
    assert shutil.which("gh").startswith(str(bindir))
    return answer


def send(repo, evidence=None, key="BL-12"):
    args = ["send", "w1", "task", "--await", key, "--source", "boss", "--cwd", str(repo)]
    for pointer in evidence or []:
        args.extend(["--evidence", pointer])
    return cli.main(args)


def done():
    assert cli.main(["send", "boss", "DONE BL-12", "--source", "w1"]) == 0


def close(*extra):
    return cli.main(["await", "close", "BL-12", "--by", "probe", *extra])


def task(service):
    return service.dispatcher.tasks["BL-12"]


def flags(service):
    return [
        item.text
        for item in service.dispatcher.items.values()
        if item.source == "courier" and item.text.startswith("VERIFY")
    ]


@pytest.mark.parametrize("evidence", [None, [""]])
def test_b1_empty_evidence_refused(workers, start, repo, evidence):
    _, service = start(workers)
    assert send(repo, evidence) == cli.EXIT_FAILED
    assert not service.dispatcher.tasks
    assert not service.dispatcher.items
    assert not workers.panes["w1"].submitted


def test_b2_empty_evidence_never_done(workers, start, repo, tmp_path, capsys):
    corrupt = ledger.new("BL-12", "boss", "w1", ["file unused"], cwd=str(repo))
    corrupt["evidence"] = []
    (tmp_path / "journal.jsonl").write_text(
        json.dumps({"event": "task", "task": corrupt}) + "\n",
        encoding="utf-8",
    )
    _, service = start(workers)
    done()
    assert task(service)["state"] == "VERIFYING"
    verifier.run()
    assert task(service)["state"] == "VERIFYING"
    assert any("no evidence" in line for line in flags(service))
    with pytest.raises(SystemExit) as exc:
        close()
    assert exc.value.code == cli.EXIT_FAILED
    assert "no evidence" in capsys.readouterr().err
    assert task(service)["state"] == "VERIFYING"


@pytest.mark.parametrize("key", ["R23c-2", "BL-12", "games23#5", "Q1"])
def test_b3_key_must_be_deterministic(workers, start, repo, key, capsys):
    _, service = start(workers)
    code = send(repo, ["file out/a.txt"], key)
    if key == "R23c-2":
        assert code == cli.EXIT_FAILED
        assert "key must be a BACKLOG id, repo#n or Q number" in capsys.readouterr().err
        assert not service.dispatcher.tasks
        assert not service.dispatcher.items
    else:
        assert code == 0
        assert service.dispatcher.tasks[key]["state"] == "OPEN"


def test_b4_report_and_artifact(workers, start, repo, monkeypatch):
    _, service = start(workers)
    content = b"objective output\n"
    (repo / "out" / "a.txt").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    assert send(repo, [f"file out/a.txt {digest}"]) == 0
    done()
    assert task(service)["state"] == "VERIFYING"
    calls = []
    main = cli.main

    def traced(args):
        calls.append(args)
        return main(args)

    monkeypatch.setattr(cli, "main", traced)
    verifier.run()
    assert calls[0][:3] == ["await", "close", "BL-12"]
    assert calls[0][3:5] == ["--by", "probe"]
    assert task(service)["state"] == "DONE"
    assert task(service)["closed_by"] == "probe file"
    assert task(service)["probe_results"][0]["sha256"] == digest


def test_b5_report_without_artifact(workers, start, repo):
    _, service = start(workers)
    assert send(repo, ["file out/a.txt"]) == 0
    done()
    verifier.run()
    assert task(service)["state"] == "VERIFYING"
    assert flags(service) == ["VERIFY BL-12: evidence missing: file out/a.txt"]
    with pytest.raises(SystemExit):
        close("--evidence", "caller says file exists")
    assert task(service)["state"] == "VERIFYING"


def test_b6_artifact_without_report(workers, start, repo):
    _, service = start(workers)
    (repo / "out" / "a.txt").write_text("artifact")
    assert send(repo, ["file out/a.txt"]) == 0
    verifier.run()
    assert task(service)["state"] == "OPEN"
    assert flags(service) == ["VERIFY BL-12: artifact found, no report"]
    with pytest.raises(SystemExit):
        close()
    assert task(service)["state"] == "OPEN"


def test_b7_pr_open_not_merged(workers, start, repo, fake_gh):
    _, service = start(workers)
    assert send(repo, ["pr games23#5"]) == 0
    done()
    verifier.run()
    assert task(service)["state"] == "DONE"
    assert task(service)["closed_by"] == "probe pr"
    assert task(service)["probe_results"][0]["state"] == "OPEN"
    assert task(service)["probe_results"][0]["merged"] is False


def test_b8_pr_closed_or_probe_error(workers, start, repo, fake_gh, monkeypatch):
    _, service = start(workers)
    assert send(repo, ["pr games23#5"]) == 0
    done()
    fake_gh("CLOSED")
    verifier.run()
    assert task(service)["state"] == "VERIFYING"
    assert flags(service) == ["VERIFY BL-12: PR not open or merged: pr games23#5"]
    now = task(service)["sent"]
    monkeypatch.setattr(ledger.time, "time", lambda: now)
    fake_gh(error=True)
    for _ in range(3):
        verifier.run()
    assert task(service)["state"] == "VERIFYING"
    assert flags(service).count("VERIFY BL-12: probe error: gh failed") == 1
    monkeypatch.setattr(ledger.time, "time", lambda: now + 86400)
    verifier.run()
    assert flags(service).count("VERIFY BL-12: probe error: gh failed") == 2
    assert task(service)["state"] == "VERIFYING"


@pytest.mark.parametrize("report", [False, True])
def test_b9_log_line(workers, start, repo, report):
    _, service = start(workers)
    (repo / "LOG.md").write_text("start\nDONE FABLE-R23c\nend\n")
    assert send(repo, ['log LOG.md "DONE FABLE-R23c"']) == 0
    if report:
        done()
    verifier.run()
    assert task(service)["state"] == "DONE"
    assert task(service)["closed_by"] == "probe log"


def test_b10_single_writer(workers, start, repo, monkeypatch, tmp_path):
    _, service = start(workers)
    (repo / "out" / "a.txt").write_text("artifact")
    assert send(repo, ["file out/a.txt"]) == 0
    done()
    d = service.dispatcher
    record = d._task_record
    closes = []
    main_thread = threading.get_ident()

    def recorded(value):
        if value["state"] == "DONE":
            assert threading.get_ident() != main_thread
            assert value["closed_by"].startswith("probe ")
            closes.append(dict(value))
        record(value)

    monkeypatch.setattr(d, "_task_record", recorded)
    with pytest.raises(Refused, match="no route"):
        request("POST", "await/append", {"key": "BL-12", "state": "DONE"})
    with pytest.raises(Refused, match="close requires --by probe"):
        request("POST", "await/close", {"key": "BL-12", "by": "worker"})
    verifier.run()
    assert len(closes) == 1
    lines = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    terminal = [
        line for line in lines if line.get("event") == "task" and line["task"]["state"] == "DONE"
    ]
    assert len(terminal) == 1
    assert terminal[0]["task"]["closed_by"] == "probe file"


def test_b11_verifier_reads_only(workers, start, repo, fake_gh, tmp_path):
    _, service = start(workers)
    (repo / "out" / "a.txt").write_text("artifact")
    assert send(repo, ["file out/a.txt", "pr games23#5"]) == 0
    done()

    def snapshot():
        return {
            str(path.relative_to(repo)): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    journal = (tmp_path / "journal.jsonl").read_bytes()
    verifier.run()
    assert task(service)["state"] == "DONE"
    assert snapshot() == before
    assert (tmp_path / "journal.jsonl").read_bytes() != journal


def test_server_rechecks_after_verifier_observation(workers, start, repo, monkeypatch):
    _, service = start(workers)
    path = repo / "out" / "a.txt"
    path.write_text("artifact")
    assert send(repo, ["file out/a.txt"]) == 0
    done()
    main = cli.main

    def removed(args):
        path.unlink()
        return main(args)

    monkeypatch.setattr(cli, "main", removed)
    outcome = verifier.run()[0]
    assert outcome["close_exit"] == cli.EXIT_FAILED
    assert task(service)["state"] == "VERIFYING"


@pytest.mark.parametrize(
    "evidence",
    [
        ["file out/a.txt " + "0" * 64],
        ['log LOG.md "DONE FABLE-R23c"'],
        ["test arbitrary-command"],
    ],
)
def test_nonmatching_evidence_never_closes(workers, start, repo, evidence):
    _, service = start(workers)
    (repo / "out" / "a.txt").write_text("artifact")
    (repo / "LOG.md").write_text("DONE FABLE-R23c extra text\n")
    assert send(repo, evidence) == 0
    done()
    verifier.run()
    assert task(service)["state"] == "VERIFYING"


def test_mixed_log_and_file_requires_report(workers, start, repo):
    _, service = start(workers)
    (repo / "out" / "a.txt").write_text("artifact")
    (repo / "LOG.md").write_text("DONE FABLE-R23c\n")
    assert send(repo, ['log LOG.md "DONE FABLE-R23c"', "file out/a.txt"]) == 0
    verifier.run()
    assert task(service)["state"] == "OPEN"
    assert flags(service) == ["VERIFY BL-12: artifact found, no report"]


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"state": "MERGED", "mergedAt": "2026-10-29T00:00:00Z"}, "DONE"),
        ({"body": "tests passed"}, "VERIFYING"),
        ({"url": "https://github.com/NaNIsaac/other/pull/5"}, "VERIFYING"),
        ({"url": "https://github.com/NaNIsaac/games23/pull/6"}, "VERIFYING"),
    ],
)
def test_pr_metadata_is_checked(workers, start, repo, fake_gh, fields, expected):
    _, service = start(workers)
    assert send(repo, ["pr games23#5"]) == 0
    done()
    fake_gh(**fields)
    verifier.run()
    assert task(service)["state"] == expected
    if expected == "DONE":
        assert task(service)["probe_results"][0]["merged"] is True
    else:
        assert len(flags(service)) == 1
