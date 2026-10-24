# herdr-console

A planned terminal-first **human console and prompt dispatcher** for [herdr](https://github.com/search?q=herdr&type=repositories) workspaces. Its primary purpose is to keep human typing separate from agent-generated prompts and make terminal/SSH operation possible without a browser.

> **Status:** Initial project outline only. No CLI or dispatcher implementation is present in this repository yet.

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
