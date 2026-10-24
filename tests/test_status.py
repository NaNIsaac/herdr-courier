from herdr_console import cli, display

NOW = 1_800_000_000.0


def reply(*, alive=True):
    return {
        "worker": {
            "alive": alive,
            "seconds_since_tick": 0.4 if alive else 812.0,
            "errors": 3,
            "last_error": "HerdrError: herdr agent read w1:p1S timed out after 15 s (exit 124)",
        },
        "status": {
            "panes": {
                "w1:p1S": {
                    "hold": {
                        "reason": "isaac is typing a long instruction on the phone",
                        "owner": "console",
                        "seconds_left": 118,
                        "restored": True,
                    },
                    "observed": "working, not empty: 4 characters after the prompt marker",
                    "pending": [
                        {
                            "id": "870ad8d33d",
                            "state": "queued",
                            "created": NOW - 75,
                            "human": True,
                            "source": "human-console",
                            "reason": "held by console until the hold expires or is released",
                            "text": "[human-console] HC7 PR 열림: hc status를 62열에 맞춤, 테스트 추가",
                        }
                    ],
                },
                "w1:p45": {
                    "hold": None,
                    "observed": "idle, empty: prompt line blank",
                    "pending": [],
                },
            },
            "recent": [
                {
                    "id": "37c7b8e85e",
                    "state": "sent",
                    "updated": NOW - 30,
                    "target": "human-console",
                    "reason": "agent working after submit",
                    "attempts": 0,
                },
                {
                    "id": "dc5a618e5c",
                    "state": "failed",
                    "updated": NOW - 20,
                    "target": "xrr-policy",
                    "reason": "input line not empty after 40 checks",
                    "attempts": 40,
                },
            ],
        },
    }


def test_every_line_fits_a_phone():
    lines = cli.render_status(reply(alive=False), width=62, recent=10, now=NOW)
    assert lines[0].startswith("WARNING: dispatcher worker is not running")
    assert max(display.cols(line) for line in lines) <= 62


def test_wrapping_keeps_every_word():
    lines = cli.render_status(reply(), width=62, recent=10, now=NOW)
    words = " ".join(lines).split()
    for phrase in (
        "isaac is typing a long instruction on the phone",
        "restored after restart",
        "held by console until the hold expires or is released",
        "input line not empty after 40 checks (attempts 40)",
        "HerdrError: herdr agent read w1:p1S timed out after 15 s (exit 124)",
    ):
        assert phrase in " ".join(words)


def test_columns_and_continuations():
    lines = cli.render_status(reply(), width=62, recent=10, now=NOW)
    pending = lines.index(next(line for line in lines if "870ad8d33d" in line))
    assert lines[pending].startswith("  870ad8d33d queued      75s human from human-console:")
    assert lines[pending + 1].startswith("      ")
    sent = next(line for line in lines if "37c7b8e85e" in line)
    assert sent.endswith("37c7b8e85e sent      human-console: agent working" + display.ELLIPSIS)
    assert lines[lines.index(sent) + 1].startswith("  ")
    failed = next(line for line in lines if "dc5a618e5c" in line)
    assert failed.endswith("dc5a618e5c failed    xrr-policy: input line not")
    assert lines[lines.index(failed) + 1] == "    empty after 40 checks (attempts 40)"


def test_message_preview_is_one_line_cut_by_cells():
    lines = cli.render_status(reply(), width=40, recent=10, now=NOW)
    preview = next(line for line in lines if "[human-console]" in line)
    assert preview.startswith("      [human-console] HC7")
    assert preview.endswith(display.ELLIPSIS)
    assert display.cols(preview) <= 40


def test_wide_terminal_keeps_one_line_per_item():
    lines = cli.render_status(reply(), width=200, recent=10, now=NOW)
    assert sum("37c7b8e85e" in line for line in lines) == 1
    assert "    after submit" not in lines


def test_empty_status():
    empty = {"worker": {"alive": True}, "status": {"panes": {}, "recent": []}}
    assert cli.render_status(empty, width=62, recent=10, now=NOW) == ["no queued messages or holds"]


def test_wrap_cuts_a_word_longer_than_the_line():
    lines = display.wrap("x" * 80, 20, "  ", "  ")
    assert lines == ["  " + "x" * 17 + display.ELLIPSIS]
