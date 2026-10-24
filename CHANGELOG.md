# Changelog

## 0.2.0

- Renamed from herdr-console: the package is `herdr-courier`, the module `herdr_courier`, the
  commands `courier` and `courierd`. `hc` and `hcd` remain as short names for the same programs.
- Environment variables are `COURIER_STATE_DIR` and `COURIER_SOURCE` (were `HC_*`). The state
  directory is `herdr-courier`; `courierd` moves a `herdr-console` directory on its first start.
- New commands: `courier agents` (panes with tab, agent name, status and queue), `courier panes`
  (panes by tab with label and working directory) and `courier doctor` (read-only health check).
- `courier status` fits the terminal width, 62 columns when it cannot tell.
- `config.toml` in the state directory overrides the pane label rule, the working-directory
  variables, the other-writer endpoint files and the Ovis URL; `config.example.toml` lists the
  defaults.

## 0.1.0

- `hcd`, the prompt dispatcher, and `hc send`, `status`, `hold`, `release` and `cancel`.
