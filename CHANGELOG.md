# Changelog

## Unreleased

- courier knows its own text in an input box also when the box wrapped it onto more rows or shows
  it as a `[Pasted text #N +K lines]` placeholder, and presses Enter again on a ghost every minute,
  up to 3 more times, while the agent is idle and unfocused (a reader's pointer line: focused too)
  and the box holds exactly that text.
- A mailbox reader's focused pane gets its pointer line once the box has stayed empty for 30 s,
  since one pane is always focused, often an idle agent nobody watches.
- `courier inbox --show ID` marks the message shown, so a reader without a live hook ends the item
  by reading it.
- A message filed while its reader was working gets its pointer line about 10 s after the reader
  goes idle when no Stop hook has shown it, instead of after the 5 minute re-wake.

- A message for a mailbox reader is `sent` only when the reader's hook has shown it. Until then it
  is `filed`: a reader that is working or has other text in its input box gets no pointer line and
  no longer holds up the messages behind it, a pointer line left in the box keeps its message
  filed instead of lost, an unshown message gets its pointer typed again up to 3 times, and filed
  messages survive a restart.
- `courier inbox --hook` on Stop hands the agent its new messages at the end of its turn, so a
  working agent gets its mail without anything typed into its pane.

- courier's own notices to a mailbox reader, such as a ghost report, are filed in its mailbox
  before their pointer line is typed; before, the pointer led to nothing and the text was lost.
  A notice that cannot be filed is typed in full.

## 0.3.0

- With the mailbox on, an agent in `mailbox.readers` gets one line per message in its pane,
  `[courier] <id> from <sender>: in your mailbox, courier inbox --show <id>`, and its hook adds
  the text, so the agent no longer gets each message twice. The hook names an id that comes again
  instead of showing its text twice, and one not filed yet. Slash commands are still typed whole.

- A ghost is reported once to the notice receiver, or to the controller when the ghost is in the
  notice receiver's box, so its text is cleared before someone types into that box and sends
  both. `courier send` refuses a message ending in a backslash, which Claude Code takes as a
  line break and leaves unsent.

- Messages waiting for the same ready pane go in one prompt, oldest first, each under a
  `--- <id> from <sender> at HH:MM ---` line, up to `delivery.merge_chars` (6000) characters.
  Slash commands go alone, human messages merge only with each other, and each message keeps
  its id, state and mailbox row. `merge_chars = 0` types each one alone.

- Add `ppw where NAME` to show a tab, pane label and pane id by exact agent name,
  pane label or service label suffix. `--focus` focuses an agent pane, or a service's
  tab when no agent runs there. Ambiguous names never change focus.
- Distinguish receiver `rejected` usage-limit responses and unsubmitted `ghost` text from sent.
  Exact single/multiline pastes get at most one guarded Enter retry; recent ghost/rejected
  records survive restart without replay. Known Codex placeholder/footer and complete queued
  multiline layouts have bounded input-detection fallbacks.
- Add `courier submit TARGET`: the sole dispatcher worker may press Enter only for complete
  retained courier text under fresh receiver/input/hold/focus guards. It never types text,
  accepts a shared prefix or replays a key after restart. Doctor and send --wait handle the
  new outcomes. Upgrade client and serve together.

- `install.ps1` and `install.sh`: one line installs courier on a machine with nothing on it. They
  install uv if it is missing, then install the main branch from its source archive, so git is
  not needed either.

- `courier route NAME TO` sends every message for agent NAME to agent TO, as when a deputy
  stands in for boss; `courier route NAME --clear` drops it and `courier route` lists the routes.
  Queued messages not yet typed in follow a change. A target that is not running, closes or
  starts another agent sends its messages back to NAME. Loops and pane ids are refused. Only
  the controller, the secretary, a herdr pane with no agent or a terminal outside herdr may
  change a route; the daemon
  keeps `routes.json` and writes each change to `routes.log`. `status` and `agents` show the
  routes, and the deputy passes the controller check while routed.

- `courier send --file PATH` (repeatable) copies files into this PC's inbox folder,
  `<inbox>/YYYYMMDD-HHMMSS-<message id>/`, and adds one line per file naming the copy, with its
  origin, size and SHA-256 prefix as information. `MACHINE:PATH` fetches with scp. At most 10
  files, 25 MB each, 100 MB in all; any failed copy sends nothing and removes the copies. The
  mailbox records them as `attachments`; `files.inbox` sets the folder.

- Retry Enter once for a confirmed pending multiline paste, without repasting its text.
  Fresh identity, hold, focus and input checks guard recovery; restarts never replay the key.

- Recognise the Antigravity CLI (agy) input box (rules, "> " prompt, dim placeholder) so
  `send` can deliver to an agy pane; dialogs inside the rules stay unknown.

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
