# herdr-console

A planned terminal-first **human console and prompt dispatcher** for [herdr](https://github.com/search?q=herdr&type=repositories) workspaces. Its primary purpose is to keep human typing separate from agent-generated prompts and make terminal/SSH operation possible without a browser.

> **Status:** `hcd` (the prompt dispatcher), `hc send/status/hold/release/cancel` and `hc agents` work; the dispatcher is tested against a real herdr pane. Pane management and `hc doctor` are not written yet.

Agents send with `hc send <target> <text>` instead of `herdr agent prompt`. hcd queues the message per pane and types it only when the target's input box is empty, so a person's half-typed message is never submitted with it. Design and send conditions: [docs/DISPATCH.md](docs/DISPATCH.md).

## Install

```sh
uv tool install git+https://github.com/NaNIsaac/herdr-console
```

## Run

> **Not yet as a permanent service.** Until isaac-control-center stops running its own in-process dispatcher (its PR that switches it to read hcd), do not keep hcd running: icc's dispatcher defaults to on and uses another lock directory, so two writers could type into the same pane, and both packages install an `hc` command.

Start the dispatcher in a herdr shell pane (not in an agent's background shell), then send from anywhere on the same machine:

```sh
hcd
hc send boss "[lane] status report"
hc send boss "[lane] status report" --wait --timeout 120000
hc status
hc hold boss --reason "typing on the phone" --ttl 600
hc release boss
```

`hc status` wraps to the terminal width (62 columns when it cannot tell; `--width` sets it), so a
phone shows every line whole. Delivered items in its recent log take one line each; failures wrap.

`hc agents` needs no hcd. It lists every pane with its tab label, live agent name, status and
hcd queue, sized for a phone terminal, and warns when a tab is not named after the agent it holds.
herdr's mobile layout (62-column Termux screens fall under the default `ui.mobile_width_threshold`
of 64) shows only the tab label in its header, never the pane or agent name, so the tab label is
what a phone user goes by.

```sh
hc agents            # table for the current terminal width
hc agents --wide     # adds the terminal title column
hc agents --json
```

`hc panes` lays the workspace out by tab: each pane's label and working directory (`$DEV`,
`$DATA` and `~` spelled out, long paths cut from the left). It warns when a pane label breaks the
team's naming rule: `core`, `isaac` and `lane` panes are `<prefix> · <tab>`, `infra` panes
`infra · <process>`, and a worker a lane starts `<tab> › <task>`; each warning carries the
`herdr pane rename` command that fixes it.

```sh
hc panes
hc panes --json
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
