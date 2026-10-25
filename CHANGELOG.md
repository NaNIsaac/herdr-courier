# Changelog

## Unreleased

- Messages for the agents in `ovis.panes` (default `secretary`) go only between turns while Ovis
  answers at `ovis.url`: never while the agent works, and only after its state has stayed
  unchanged for `ovis.settle` seconds (default 3). A prompt delivered during a turn started the
  next turn just as Ovis sent the chat message queued in that turn, and Ovis then left its paste
  unsent in the input box. Without Ovis nothing changes.
- `doctor.ovis_url` is now `ovis.url`; a `config.toml` that still sets `doctor.ovis_url` is
  reported as an unknown key. `courier serve` now reads `config.toml` and refuses to start when
  it is invalid.
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
