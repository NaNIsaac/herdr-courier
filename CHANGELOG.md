# Changelog

## Unreleased

- Objective file/hash, exact-log, and PR probes through `await verify` and `await close --by
  probe`. A manual foreground `--watch` defaults to 600 s and 100 probes, including closing
  rechecks. Changed tasks defer closure; matching all-log evidence can close without a reply.
  Scheduling and S1 integration are not configured.
- Order journal generation replay and pruning by UTC filename stamp and numeric suffix,
  so copying or restoring files cannot reorder history through changed mtimes.

- Durable tasks through `send --await`, evidence pointers, `await list`, configurable compact
  hook recovery, and read-only serve-down fallback. Pending manual dispatches replay without
  typing. Duplicate dispatches become bounded reminders; task reports enter VERIFYING.
- Controller-pane checks for task reassign/cancel, required force reasons, and durable
  secretary notices. Retention compacts terminal details while preserving closed-key guards
  and attempts, warns at 80%, and reports capacity refusals explicitly.
- Recover pending messages, holds, and task records from retained generations if rotation
  stops before the replacement snapshot is complete; finished deliveries remain final (#25).

- A message for an agent whose pane shows no session is held for up to 4 h (`hold.ttl`) and
  delivered when the same session is back, instead of failing at once with `identity_changed`.
  Another session still fails it.
- Agents held while missing (`[hold]`, default `boss` and `secretary`): a message for one that
  herdr does not know, or whose pane is gone or lost its session, is held by name instead of
  refused or failed. When it is back with the same session the 10 oldest are typed in, then one
  line about the rest; with another session they stay held. The other agent of the pair gets one
  summary of what is held, at most every 15 min. `courier status` lists held messages by name.
  See docs/DISPATCH.md.

- Mailbox, off by default (`mailbox.write`, `mailbox.readers`): courier serve also appends each
  queued message to `mailbox/<target>.jsonl` with the journal's id, and `courier inbox` lists the
  messages, prints them in full and marks them done. `courier inbox --hook` answers a Claude Code
  UserPromptSubmit or SessionStart hook with the open messages as additional context. Typing
  into panes is unchanged. See docs/mailbox.md.
- `courier send` warns when the text starts with `C:/Program Files/Git/`, which is what Git Bash
  makes of a leading `/` such as `/effort`.
- Messages for the agents in `ovis.panes` (default `secretary`) go only between turns while Ovis
  answers at `ovis.url`: never while the agent works, and only after its state has stayed
  unchanged for `ovis.settle` seconds (default 3). A prompt delivered during a turn started the
  next turn just as Ovis sent the chat message queued in that turn, and Ovis then left its paste
  unsent in the input box. Without Ovis nothing changes.
- `doctor.ovis_url` is now `ovis.url`; a `config.toml` that still sets `doctor.ovis_url` is
  reported as an unknown key. `courier serve` now reads `config.toml` and refuses to start when
  it is invalid.
- `courier serve` is bound to the herdr server its CLI reaches when it starts, and the journal
  records it. It refuses to start when the journal holds queued messages or holds for another
  server, since pane ids are numbered per server. `courier send`, `hold` and `release` from a pane
  of another herdr server are refused (409), and `send --wait` waits on courier serve's server.
- A message for an agent with no session is tied to the pane's terminal and fails instead of being
  delivered when the pane or the server restarted.
- Each `courier serve` start keeps the previous journal as `journal.<UTC time>.jsonl` and the
  newest 10 of them, instead of overwriting `journal.prev.jsonl`, so delivery history survives
  more than one restart. An existing `journal.prev.jsonl` is left in place.

## 0.2.0

- Renamed from herdr-console: the package is `herdr-courier`, the module `herdr_courier`, the
  command `courier`. `hc` remains as a short name for the same program.
- The daemon is `courier serve`. `hc serve` and `hcd` are the same command, so there is one
  program and one entry point to the dispatcher.
- Environment variables are `COURIER_STATE_DIR` and `COURIER_SOURCE` (were `HC_*`). The state
  directory is `herdr-courier`; `courier serve` moves a `herdr-console` directory on its first
  start.
- New commands: `courier agents` (panes with tab, agent name, status and queue), `courier panes`
  (panes by tab with label and working directory) and `courier doctor` (read-only health check).
- `courier agents --color[=auto|always|never]`: status colours, bold agent names, waiting queues in
  yellow, shell panes dimmed. `auto` (the default) colours a terminal unless `NO_COLOR` is set.
- `courier status` fits the terminal width, 62 columns when it cannot tell.
- `config.toml` in the state directory overrides the pane label rule, the working-directory
  variables, the other-writer endpoint files and the Ovis URL; `config.example.toml` lists the
  defaults.

## 0.1.0

- `hcd`, the prompt dispatcher, and `hc send`, `status`, `hold`, `release` and `cancel`.
