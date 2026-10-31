# Prompt dispatcher

Agents in a herdr workspace message each other with `herdr agent prompt <pane> <text>`. That
command pastes the text after whatever is already in the target's input box and presses Enter, so
a half-typed human message is submitted together with the agent's text. This happened in a Claude
Code pane on herdr 0.9.5; `tests/integration/test_live_pane.py` reproduces the collision against a
dummy agent. The dispatcher is the one path that writes prompts into panes, and it writes only
when the input box is empty.

People type in Ovis (the herdr browser and phone client) or in the herdr TUI. Ovis keeps a
person's draft in the browser and checks the box itself before sending. The dispatcher covers
the other side: agents and scripts, through `courier send`.

Ovis also queues a chat message written during a turn and sends it when the turn ends. That send
pastes, waits 120 ms and gives up before Enter if the agent has started another turn by then,
leaving the paste unsent in the box (Ovis 0.4.14, `server/core/pending.ts`). A prompt the
dispatcher delivered during the same turn starts that next turn: Claude Code queues it and runs it
as soon as the turn ends. Ovis tells no other program which panes hold such messages, so for the
agents in `ovis.panes` the dispatcher writes only between turns (condition 5).

## Layout

```text
src/herdr_courier/
  core/inputline.py   classify a Claude Code input box from `herdr pane read --format ansi`
  core/gate.py        the send conditions as a pure function of one observation
  core/queue.py       Dispatcher: per-pane FIFO, holds, delivery state machine, JSONL journal
  core/transport.py   the only code that writes into panes (agent prompt; send-text for tests)
  daemon.py           courier serve: instance lock, endpoint file, worker thread, /api/dispatch/*
  client.py           standard-library client for those routes (courier and other programs)
  cli.py              courier
  agents.py           courier agents: panes joined with tabs, agent names and the queue
  panes.py            courier panes: panes by tab, labels checked against a naming rule
  doctor.py           courier doctor: read-only checks of herdr, courier serve and its neighbours
  ovis.py             whether Ovis answers, and the between-turns rule for `ovis.panes`
  defaults.py         built-in settings; config.toml in the state directory overrides them
  config.py           reads and validates config.toml
  state.py            state directory: journal, lock, endpoint file, config
```

Everything is standard library; the only external program is the `herdr` CLI. Other programs
that need the queue use `client.py` rather than writing into panes themselves.

## Process model

`courier serve` is the one long-running process that writes to panes. Start it in a herdr shell
pane so it outlives agent sessions. One worker thread calls `Dispatcher.tick()` every 0.5 s; it is
the only writer to any pane, which is what makes each per-pane queue a lock. Request threads only
enqueue, hold, release, cancel, wait and read status.

The state directory is `%LOCALAPPDATA%\herdr-courier` on Windows and
`$XDG_STATE_HOME/herdr-courier` (default `~/.local/state/herdr-courier`) elsewhere;
`COURIER_STATE_DIR` overrides it. A 0.1 directory (`herdr-console`) is moved to the new name on
the first start when the new one does not exist yet. Before it reads the journal, `courier serve`
takes an OS file lock on `<state dir>/lock`, so a second `courier serve` exits with a message; the
OS releases the lock when the process dies. After replaying the journal it renames it to
`journal.<UTC time>.jsonl` and starts a new one; the newest 10 of those generations are kept.

On startup `courier serve` writes `<state dir>/endpoint.json` with its port (any free port unless
`--port` is given), a fresh random token and its pid, and removes it on clean shutdown if it still
holds its own token. On POSIX the file is created with mode 0600; on Windows it inherits the ACL
of the user's profile directory, so SYSTEM and local administrators can read the token too.
Anyone who can read it can queue prompts. `courier` reads that file and calls
`http://127.0.0.1:<port>/api/dispatch/...` with `Authorization: Bearer <token>`. The server
listens on 127.0.0.1 only, refuses a `Host` other than localhost (so a web page that points its
own name at 127.0.0.1 cannot reach it), requires the token on every route, and sends no CORS
headers.

## One herdr server

A herdr server is known by its socket: `HERDR_SOCKET_PATH` when set, else the default session's;
`herdr --session <name>` picks a named session's. The working directory plays no part. A socket
with no server behind it fails with `server_not_running`; herdr never falls back to another
server. Pane ids are numbered per server, so two servers both have `w1:p1`, and a server keeps
its pane ids across a restart while giving every pane a new `terminal_id` (herdr 0.9.5, read on
isolated named sessions).

`courier serve` asks `herdr status server` for its socket when it starts and calls only that
server for its whole life. The journal records the socket. A start that reaches another socket
while the journal still holds queued messages or holds exits with a message instead of replaying
them against the other server's panes; with nothing left it takes the new server. While the server
is down, messages wait and new ones are refused; when the same socket answers again they go on.

`courier send`, `hold` and `release` from inside a herdr pane send that pane's
`HERDR_SOCKET_PATH`, and `courier serve` refuses a request from another server with 409: its
names and pane ids mean other panes there. A caller outside herdr names no server and is taken to
mean courier serve's. `send --wait` runs `herdr agent wait` against courier serve's server.

If `courier serve` is not running, `courier send` exits with status 3, says nothing was sent, and
prints the manual path (read the target's input box, then `herdr agent prompt`). It never falls
back to writing into the pane itself.

The worker loop survives an exception in one pass; `/api/dispatch/status` reports whether the
worker is alive, seconds since its last pass and the last error, and `courier status` warns when
it is not running.

## Send conditions

All of these, observed again right before writing (a second `agent get` and box read):

1. Agent status is `idle`, `done` or `working` (Claude Code queues a prompt that arrives while it
   works). `blocked` and `unknown` wait.
2. The input box reads empty: no text after the prompt glyph, or only dim placeholder text, for
   1 s in a row (3 s when the pane is focused). No recognisable box, as with a dialog that herdr
   reports as idle, waits.
3. No hold on the pane (`courier hold`).
4. The pane still runs the agent session the message was queued for; a different session fails
   the item instead of delivering it to someone else. A pane that shows no session may be the same
   agent restarting, so the item waits (`hold: identity_changed: ...`) for that session to come
   back, for at most `hold.ttl` (4 h), and then fails. A message queued for an agent with no
   session is tied to the pane's `terminal_id` instead and fails when the pane has a new terminal
   (the pane or the server restarted). Names are resolved once, when the message is queued: a
   name that moves to another pane does not take the message along, except for the agents below.

## Agents held while missing

For the agents in `hold.when_missing` (default `boss` and `secretary`) courier holds instead of
refusing or failing:

- A message for one that herdr does not know is held (`hold: boss missing since 13:20`) instead
  of refused, and `courier send` prints `held <id>`. A queued message whose pane is gone, shows no
  session or runs another session is held the same way, by name.
- Every 5 s courier looks the name up. When it is back with the session the messages were queued
  for (a resume, in any pane), the 10 oldest are queued to it and, if more are held, one line
  `courier: N more held for you: courier inbox` follows them. Back with another session, nothing
  is typed in; the held messages stay in `courier status` and, with the mailbox on, in
  `courier inbox`. A pane without a session counts as missing.
- A held message fails after `hold.ttl` seconds (4 h).
- While messages are held, the agent named in `hold.summary_to` (boss for secretary, secretary
  for boss) gets one summary, such as `courier: boss missing since 13:20; held 3 (design 2,
  icc 1); oldest 13:21`: at once, then at most every `hold.summary_every` seconds (15 min) while
  the count changes, and once when the agent is back. Failed ones are counted in the next one.
  Nothing is sent while that agent is missing too, and summaries are never held themselves.
- Held messages and their deadlines are journalled and still held after a restart.
5. For an agent named in `ovis.panes`, while Ovis answers its health route (asked at most every
   30 s): the agent is not `working`, and its state (status and herdr's `state_change_seq`) has
   not changed for `ovis.settle` seconds, 3 by default. A queued Ovis message goes out within
   that time after the turn ends, starting a new turn that this message then waits out. 3 s
   outlasts courier's own measured write-to-`working` time (p95 0.96 s, max 1.69 s over 400 sends)
   plus Ovis' fixed 0.42 s of waits. Without Ovis, or for other agents, nothing changes.

Right before writing, the item is journalled as `submitting` while the queue lock is held. From
then on it is never written again: a cancel is refused, and if the process dies the next start
replays it as failed ("delivery unknown") instead of queuing it. Holds are journalled too and
come back after a restart, marked as restored in `courier status`.

After writing, a working state, an empty box or the queued-messages placeholder within 5 s
confirms delivery, and the next message to that pane waits a full debounce again. Text left in
  the box fails the item without a resend. For a multiline message still fully visible in the
  input box, courier rechecks the session, terminal, hold and focus before retrying Enter once.
  The target must be idle or done; working alone is not confirmation while that same text remains.
  The retry is journalled before the key and is never replayed after restart. Courier does not
  repaste the message or retry an edited, wrapped, collapsed or otherwise unproven input box.
  A second verification timeout fails the item. Only errors that prove
nothing was written (`agent_blocked`, `agent_not_ready`) are retried, at most 5 times with
backoff. Any other failure during the write fails the item.

`courier send` gives each request a random `client_id`. If the reply is lost, `courier` retries
once with the same id and gets the first item back instead of queuing the message twice.

## Run

```powershell
courier serve           # in a herdr shell pane, so it outlives agent sessions
courier send boss "[lane] report"
courier status
```
