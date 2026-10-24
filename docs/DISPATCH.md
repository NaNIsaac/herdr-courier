# Prompt dispatcher

Agents in the herdr workspace used to message each other with `herdr agent prompt <pane> <text>`.
That command pastes the text after whatever is already in the target's input box and presses
Enter, so a half-typed human message was submitted together with the agent's text. The dispatcher
is the one path that writes prompts into panes, and it writes only when the input box is empty.
The reproduction and the herdr facts behind each rule are in the HC1 investigation in the coordination notes (`$DEV/NaNIsaac/docs/human-console/INVESTIGATION.md`).

People type in Ovis (the herdr browser and phone client) or in the herdr TUI. Ovis keeps a
person's draft in the browser and checks the box itself before sending. The dispatcher covers
the other side: agents and scripts, through `hc send`.

## Layout

```text
src/herdr_console/
  core/inputline.py   classify a Claude Code input box from `herdr pane read --format ansi`
  core/gate.py        the send conditions as a pure function of one observation
  core/queue.py       Dispatcher: per-pane FIFO, holds, delivery state machine, JSONL journal
  core/transport.py   the only code that writes into panes (herdr agent prompt; send-text for tests)
  daemon.py           hcd: instance lock, endpoint file, worker thread, /api/dispatch/* over http.server
  client.py           standard-library client for those routes (hc and other programs)
  cli.py              hc
  agents.py           hc agents: panes joined with tabs, live agent names and the hcd queue
  state.py            state directory: journal, lock, endpoint file
```

Everything is standard library; the only external program is the `herdr` CLI. The core moved
here unchanged from Isaac Control Center, which becomes a client of hcd.

## Process model

`hcd` is the one long-running process that writes to panes. Start it in a herdr shell pane so it
outlives agent sessions. One worker thread calls `Dispatcher.tick()` every 0.5 s; it is the only
writer to any pane, which is what makes each per-pane queue a lock. Request threads only enqueue,
hold, release, cancel, wait and read status.

The state directory is `%LOCALAPPDATA%\herdr-console` on Windows and
`$XDG_STATE_HOME/herdr-console` (default `~/.local/state/herdr-console`) elsewhere; `HC_STATE_DIR`
overrides it. Before it reads the journal, `hcd` takes an OS file lock on `<state dir>/lock`, so a
second `hcd` exits with a message; the OS releases the lock when the process dies.

On startup `hcd` writes `<state dir>/endpoint.json` with its port (any free port unless `--port`
is given), a fresh random token and its pid, and removes it on clean shutdown if it still holds
its own token. On POSIX the file is created with mode 0600; on Windows it relies on the default
ACL of the user's profile directory. `hc` reads that file and calls
`http://127.0.0.1:<port>/api/dispatch/...` with `Authorization: Bearer <token>`. hcd listens on
127.0.0.1 only, refuses a `Host` other than localhost (so a web page that points its own name at
127.0.0.1 cannot reach it), requires the token on every route, and sends no CORS headers.

If `hcd` is not running, `hc send` exits with status 3, says nothing was sent, and prints the
manual path (read the target's input box, then `herdr agent prompt`). It never falls back to
writing into the pane itself.

The worker loop survives an exception in one pass; `/api/dispatch/status` reports whether the
worker is alive, seconds since its last pass and the last error, and `hc status` warns when it is
not running.

## Send conditions

All of these, observed again right before writing (a second `agent get` and box read):

1. Agent status is `idle`, `done` or `working` (Claude Code queues a prompt that arrives while it
   works). `blocked` and `unknown` wait.
2. The input box reads empty: no text after the prompt glyph, or only dim placeholder text, for
   1 s in a row (3 s when the pane is focused). No recognisable box, as with a dialog that herdr
   reports as idle, waits.
3. No hold on the pane (`hc hold`).
4. The pane still runs the agent session the message was queued for; a different or missing
   session fails the item instead of delivering it to someone else.

Right before writing, the item is journalled as `submitting` while the queue lock is held. From
then on it is never written again: a cancel is refused, and if the process dies the next start
replays it as failed ("delivery unknown") instead of queuing it. Holds are journalled too and
come back after a restart, marked as restored in `hc status`.

After writing, a working state, an empty box or the queued-messages placeholder within 5 s
confirms delivery, and the next message to that pane waits a full debounce again. Text left in
the box fails the item without a resend. Only errors that prove
nothing was written (`agent_blocked`, `agent_not_ready`) are retried, at most 5 times with
backoff. Any other failure during the write fails the item.

`hc send` gives each request a random `client_id`. If the reply is lost, `hc` retries once with the
same id and gets the first item back instead of queuing the message twice.

## Run

```powershell
hcd                   # in a herdr shell pane, so it outlives agent sessions
hc send boss "[lane] report"
hc status
```
