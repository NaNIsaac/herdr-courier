# herdr-courier

[![Tests](https://github.com/NaNIsaac/herdr-courier/actions/workflows/ci.yml/badge.svg)](https://github.com/NaNIsaac/herdr-courier/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Unofficial; not affiliated with herdr.

**A prompt queue for [herdr](https://herdr.dev) panes: agent messages wait while a person is typing.**

[한국어](README.ko.md)

When agents in a herdr workspace message each other with `herdr agent prompt`, the text is pasted
after whatever is already in the target's input box and submitted. If you were halfway through a
sentence in that pane, your half sentence goes out glued to the agent's message. courier puts a
small daemon, `courier serve`, in between: agents and scripts call `courier send`, and the daemon
types the message only after the target's input box has been empty for a moment. Each pane has
its own queue, so messages arrive in order and one at a time.

```text
$ courier send boss "build finished, 3 tests failing" --wait
   (you are typing in boss's pane: the message waits)
   (you submit your own message: the box is empty again)
sent 4f1c2a9e07 to boss (w1:p2): agent working after submit
```

## Install

courier needs [uv](https://docs.astral.sh/uv/) and the `herdr` CLI on `PATH`. It is tested on
Windows and Linux with Python 3.13, which uv fetches if needed.

<!-- test -->
```sh
uv tool install git+https://github.com/NaNIsaac/herdr-courier
uv tool update-shell
```

Open a new terminal and check the install:

<!-- test -->
```sh
courier --help
```

This installs `courier`, and `hc` as a short name for the same program (`hc send`, `hc serve`).
`hcd` is a third name for `courier serve`. Upgrade with `uv tool upgrade herdr-courier`.

## First run

Start the daemon in a herdr shell pane, so it outlives agent sessions:

```sh
courier serve
```

Then, from any other pane on the same machine:

```sh
courier doctor
courier send boss "[lane] status report"
```

`courier doctor` should print `all checks pass` on its first line. `boss` stands for an agent
name or a pane id, the same targets `herdr agent prompt` takes.

## Examples

Start the daemon; `hcd` and `hc serve` are other names for `courier serve`:

```text
$ hcd
courier serve pid 13500 on 127.0.0.1:61710, transport agent, state C:\Users\you\AppData\Local\herdr-courier
```

Report from a lane to the agent `reviewer`:

```text
$ courier send reviewer "[lane] tests pass, PR opened"
queued 6f555376a5 for reviewer (w1:p61)
```

Keep the daemon out of a pane while you type there. A message sent meanwhile waits:

```text
$ courier hold reviewer --reason "typing on the phone" --ttl 600
holding w1:p61 for 600 s
$ courier send reviewer "[lane] tests pass, PR opened"
queued 6f555376a5 for reviewer (w1:p61)
$ courier status
w1:p61  HOLD typing on the phone (lane, 599 s left)
  observed: idle, empty: dim placeholder 'Try "how does
    <filepath> work?"'
  6f555376a5 waiting      0s from lane: hold: typing on the
      phone
      [lane] tests pass, PR opened
$ courier release reviewer
released w1:p61
```

Check that everything is alive:

```text
$ courier doctor
courier doctor: all checks pass
ok   herdr      0.9.5; 13 panes, 6 agents
ok   dispatcher pid 13500, port 61710, api 1
ok   worker     last pass 0.0 s ago, no errors
ok   queue      0 queued, 0 held
ok   journal    empty (nothing queued since courier serve
                started)
ok   config     built-in defaults (no config.toml)
ok   writers    no other dispatcher running
ok   ovis       ok, revision d5a35b7, sees herdr 0.9.5;
                between turns only: secretary
```

List the panes (rows left out here); `courier agents --json` prints the same rows as JSON:

```text
$ courier agents
dispatcher: running, 0 queued, 0 held
  pane   tab               agent             status  queue
  w1:p1S boss              boss              working -
  ...
  w1:p61 reviewer          reviewer          idle    -
* = focused on the herdr server (any client)
shell = no agent in that pane
```

## Limits

- Only Claude Code's input box is recognised. A message to a pane whose box courier cannot read
  (another agent, a dialog, a changed layout) stays queued instead of being typed; drop it with
  `courier cancel <id>`.
- A queued message has no expiry. It waits until the box is empty and the pane is not held, or
  until you cancel it.
- Only messages sent through `courier send` are queued. A program that calls
  `herdr agent prompt` directly can still type over you.
- One `courier serve` per state directory, on one machine. It listens on 127.0.0.1 only.
- courier cannot see messages queued in Ovis chat; Ovis tells no other program about them. For the
  agents in `ovis.panes` it waits for the end of each turn instead, so a long turn delays
  messages to them.

## Use it

### Report from one agent to another

```sh
courier send boss "[lane] tests pass, PR opened"
courier send boss "[lane] need a decision" --wait --timeout 120000
```

Without `--wait`, `send` returns once the message is queued. With it, `send` waits for delivery
and then, like `herdr agent prompt --wait`, for the agent to settle.

### Type a long message from your phone

```sh
courier hold boss --reason "typing on the phone" --ttl 600
courier release boss
```

While a pane is held, nothing is typed into it, however long you pause. `--ttl` releases it
automatically; without it the hold lasts until `courier release`, also across a daemon restart.

### See who is where

```sh
courier agents
courier panes
```

`agents` lists every pane with its tab, agent name, status and queue. herdr's phone layout shows
only the tab label, so it warns when a tab is not named after the agent it holds. `panes` groups
panes by tab with their label and working directory, and warns when a label breaks the naming
rule in [Configuration](#configuration). Both fit a 62-column phone terminal.

In a terminal, `agents` colours the status (working green, idle grey, done blue, blocked
red), shows agent names in bold, waiting queues in yellow and shell panes dimmed. `NO_COLOR`
or `--color=never` turns this off; `--color` keeps it when the output is not a terminal, which
includes a herdr pane on Windows.

### Check that everything is alive

```sh
courier doctor
courier status
```

`doctor` changes nothing. It checks herdr, the daemon and its worker, messages waiting over 10
minutes, holds with no expiry, failures in the last hour, the journal, the config file, other
programs that type into panes, and Ovis, and suggests a fix for each problem. It exits 1 when a
check fails. `status` shows every queue and hold and what the daemon last saw in each pane.

## Reference

### Commands

| command | does |
| --- | --- |
| `courier serve [--port N] [--tick S]` | run the daemon in the foreground (port 0, the default, picks a free one); also `hc serve` and `hcd` |
| `courier send TARGET TEXT [--wait] [--until STATUS] [--timeout MS] [--source NAME] [--human]` | queue a message; `--human` puts it ahead of queued agent messages |
| `courier status [TARGET] [--recent N] [--json] [--width N]` | queues, holds and the last observation per pane |
| `courier hold TARGET [--ttl SECONDS] [--reason TEXT] [--owner NAME]` | stop the daemon from typing into a pane |
| `courier release TARGET` | drop a hold |
| `courier cancel ID` | drop a queued message |
| `courier agents [--wide] [--json] [--width N] [--color[=WHEN]]` | panes with tab, agent, status and queue |
| `courier panes [--json] [--width N]` | panes by tab with label and working directory |
| `courier doctor [--json] [--width N]` | read-only health check |

`--width` defaults to the terminal width, or 62 when it cannot tell. `--source` and `--owner`
default to `COURIER_SOURCE`, then to the calling pane's `HERDR_PANE_ID`.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | done; after `send --wait` delivers, the exit code of `herdr agent wait` |
| 1 | failed: delivery failed, a request was refused, herdr did not answer, the config is invalid, or a doctor check failed |
| 3 | the daemon is not running; nothing was sent |
| 4 | `send --wait --timeout` ran out |

### When the daemon is not running

`courier send` exits 3 and prints the steps to deliver by hand:

```sh
herdr agent read boss --source recent --lines 12 --format ansi
# in the last input box, the text after the prompt glyph must be empty or dim
herdr agent prompt boss "[lane] status report"
```

### Files

The state directory is `%LOCALAPPDATA%\herdr-courier` on Windows and
`$XDG_STATE_HOME/herdr-courier` (`~/.local/state/herdr-courier`) elsewhere; `COURIER_STATE_DIR`
moves it. It holds your `config.toml` and the daemon's `journal.jsonl`, `lock` and
`endpoint.json` (port and access token). The daemon moves a 0.1 state directory (`herdr-console`)
to the new name on its first start.

### Configuration

Every setting has a built-in default. To change one, copy
[config.example.toml](config.example.toml) to `config.toml` in the state directory and keep the
keys you change. An unknown key or a wrong type is an error, which `courier doctor` reports.

| key | default | |
| --- | --- | --- |
| `panes.separator` | `" · "` | pane labels are `<prefix> · <name>` |
| `panes.worker_separator` | `" › "` | a worker started from another pane is `<tab> › <task>` |
| `panes.named_after_tab` | `["lane", "core", "isaac"]` | prefixes whose name must be the tab label |
| `panes.named_freely` | `["infra"]` | prefixes that take any name; both lists `[]` turn the check off |
| `panes.path_variables` | `["DEV", "DATA"]` | working directories under these are shown as `$DEV\...` |
| `doctor.other_writers` | `["~/.isaac-control-center/dispatch/endpoint.json"]` | endpoint files of other programs that type into panes |
| `ovis.url` | `"http://127.0.0.1:7317"` | Ovis health route; `""` means no Ovis |
| `ovis.panes` | `["secretary"]` | while Ovis answers, messages for these agents go only between turns |
| `ovis.settle` | `3.0` | seconds the agent state must stay unchanged after a turn before such a message goes |

## How it works

The send conditions, the delivery states and the daemon's security model are in
[docs/DISPATCH.md](docs/DISPATCH.md).

## Development

From a clone:

<!-- test -->
```sh
uv sync
uv run pytest -q
```

## License

[MIT](LICENSE)
