"""A stand-in agent TUI for integration tests (Windows console).

Reads raw keys with bracketed paste on, draws an input box shaped like Claude Code's (two rules,
a prompt glyph, a dim placeholder when empty), and appends every submitted line to a JSONL log.
Ctrl+U clears the box, Ctrl+] exits.
"""

import atexit
import ctypes
import json
import msvcrt
import sys
from ctypes import wintypes
from pathlib import Path

LOG = Path(sys.argv[1])
PROMPT = "❯ "
RULE = "\x1b[38;2;136;136;136m" + "─" * 40 + "\x1b[0m"
PLACEHOLDER = '\x1b[2mTry "how does <filepath> work?"\x1b[0m'

kernel32 = ctypes.windll.kernel32
stdin = kernel32.GetStdHandle(-10)
mode = wintypes.DWORD()
kernel32.GetConsoleMode(stdin, ctypes.byref(mode))
# virtual terminal input on; line input, echo and Ctrl+C processing off
kernel32.SetConsoleMode(stdin, (mode.value | 0x0200) & ~0x0007)
# the shell that started us must get its own console mode back, even after a crash
atexit.register(kernel32.SetConsoleMode, stdin, mode.value)
# the console code page may not cover the box-drawing glyphs
sys.stdout.reconfigure(encoding="utf-8")


def log(event: dict) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def draw(buf: str, transcript: list[str]) -> None:
    # Redraw the whole screen so the box is always the last lines, as in Claude Code.
    box = PROMPT + (buf.replace("\n", "\r\n  ") if buf else PLACEHOLDER)
    history = "".join(f"{PROMPT}{t}\r\n● ok\r\n" for t in transcript[-3:])
    sys.stdout.write(f"\x1b[2J\x1b[H{history}\r\n{RULE}\r\n{box}\r\n{RULE}\r\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdout.write("\x1b[?2004h")
    buf = ""
    transcript: list[str] = []
    pasting = False
    draw(buf, transcript)
    log({"event": "ready"})
    while True:
        chunk = msvcrt.getwch()
        while msvcrt.kbhit():
            chunk += msvcrt.getwch()
        i = 0
        while i < len(chunk):
            if chunk.startswith("\x1b[200~", i):
                pasting, i = True, i + 6
                continue
            if chunk.startswith("\x1b[201~", i):
                pasting, i = False, i + 6
                continue
            c = chunk[i]
            i += 1
            if c == "\x1d":
                sys.stdout.write("\x1b[?2004l")
                return
            if c == "\x15":
                buf = ""
            elif c in "\r\n" and pasting:
                buf += "\n"
            elif c in "\r\n":
                # ConPTY may deliver Enter as CR, LF or CRLF; count a CRLF pair once.
                if c == "\n" and i >= 2 and chunk[i - 2] == "\r":
                    continue
                transcript.append(buf)
                log({"event": "submitted", "text": buf})
                buf = ""
            elif c in "\x7f\b":
                buf = buf[:-1]
            elif c != "\x1b":
                buf += c
        draw(buf, transcript)


main()
