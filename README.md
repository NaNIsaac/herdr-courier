# herdr-console

A planned terminal-first **human console and prompt dispatcher** for [herdr](https://github.com/search?q=herdr&type=repositories) workspaces. Its primary purpose is to keep human typing separate from agent-generated prompts and make terminal/SSH operation possible without a browser.

> **Status:** `hcd` (the prompt dispatcher) and `hc` (its CLI) work and are tested against a real herdr pane. Status, pane management and diagnostics commands are not written yet.

Agents send with `hc send <target> <text>` instead of `herdr agent prompt`. hcd queues the message per pane and types it only when the target's input box is empty, so a person's half-typed message is never submitted with it. Design and send conditions: [docs/DISPATCH.md](docs/DISPATCH.md).

## Install

```sh
uv tool install git+https://github.com/NaNIsaac/herdr-console
```

## Run

Start the dispatcher in a herdr shell pane (not in an agent's background shell), then send from anywhere on the same machine:

```sh
hcd
hc send boss "[lane] status report"
hc send boss "[lane] status report" --wait --timeout 120000
hc status
hc hold boss --reason "typing on the phone" --ttl 600
hc release boss
```

Only one `hcd` runs per state directory; a second one exits. If `hcd` is not running, `hc send` exits with status 3 and prints the manual path:

```sh
herdr agent read boss --source recent --lines 12 --format ansi
# in the last input box, the text after the prompt glyph must be empty or dim
herdr agent prompt boss "[lane] status report"
```

## Development

<!-- test -->
```sh
uv sync
uv run pytest -q
```

The core uses only the standard library and the `herdr` CLI.

## Responsibilities

- Inspect workspaces, panes, and agent status.
- Queue and serialize outgoing prompts to the intended pane.
- Prevent agent messages from entering an in-progress human input buffer.
- Provide clear targeting, confirmation for risky operations, and diagnostics.
- Support operator workflows over SSH, including recovery after interruptions.

## Relationship to other tools

- **herdr** provides the underlying workspace and agent sessions.
- **Isaac Control Center (ICC)** offers a unified browser view of projects and tasks; it should reuse safe control interfaces rather than independently inventing conflicting input paths.
- **Ovis** may serve as a conversational UI where appropriate. This CLI must not depend on it.
- **DotFiles** documents/bootstrap-installs tools and restores configuration on a new machine.

The console is intended to remain an independent CLI, not to become another dashboard.

## Safety and recovery design

The first implementation should use a single serialized writer per target pane, verify the target and expected pane state before sending, keep queue/retry events inspectable, and never assume an empty screen means that the user is not typing.

Document installation, operating-system support, prerequisites, backups of non-secret configuration, and a tested fresh-machine bootstrap before claiming disaster recovery. Do not store tokens or private session content in Git.

## Roadmap

1. Inventory existing herdr commands and ICC dispatcher behavior to avoid duplicating active work.
2. Define a small testable queue/dispatch API and simulate concurrent human/agent input.
3. Implement read-only status commands and safe opt-in dispatch.
4. Add CLI tests, SSH usage notes, and failure/restart recovery tests.

No commands described above should be taken as already implemented.
