# Mailbox

courier types each message into the target's pane. The journal then says whether the text was
typed, but not whether the agent saw it, and a message typed while the agent compacts or restarts
can be lost from its context. The mailbox keeps a second copy of each message outside the agent's
context until the agent marks it done. It is off by default. When it is on, an agent in
`mailbox.readers` gets only a pointer line per message in its pane, and its hook brings the text
(see [Pointer lines](#pointer-lines)); other agents get the text typed as before.

## Files

With `mailbox.write = true` in `config.toml`, courier serve appends to
`<state dir>/mailbox/<target>.jsonl` each time it queues a message. The target is lowercased, and
characters other than letters, digits, `.`, `_` and `-` become `_`, so `w3:pC` is `w3_pc.jsonl`.
A message sent to a pane id is filed under that id, not under the agent's name.

Each line is one JSON record:

| kind | written by | fields |
| --- | --- | --- |
| `message` | courier serve, when it queues the item | `id` (the journal's), `to`, `from`, `created`, `human`, `text`, and `attachments` when the message came with `courier send --file` |
| `shown` | `courier inbox --hook`, when it puts the message into the agent's context | `id`, `at`, `event` |
| `done` | `courier inbox --ack ID` | `id`, `at` |

Each attachment is `{path, from_machine, from_path, size, sha256}`: the copy on this PC, the
machine and path it came from, its size in bytes and its SHA-256.

A message is `new` until it is shown, then `read`, then `done`. A client retry that returns the
same queued item adds no second `message` line. A line cut short by a crash is skipped.

`mailbox/hook.log` has one line per hook call, including calls with nothing to show: time, agent,
event, the ids shown (`-` for none) and the bytes of context added.

## Reading

```sh
courier inbox            # this pane's agent: every message not done, in full
courier inbox boss --all # another agent's, done ones included
courier inbox --show ID  # one message in full; marks it shown, like the hook
courier inbox --ack ID   # mark done
```

Without a name, `courier inbox` uses the herdr agent name of the calling pane (`HERDR_PANE_ID`
matched in `herdr agent list`).

## Claude Code hook

`courier inbox --hook` reads the hook payload on stdin. For an agent listed in `mailbox.readers`, it
prints the open messages as `additionalContext` and records them as shown:

- on UserPromptSubmit, the `new` ones;
- on SessionStart (after a compaction, resume or restart), every one not `done`, since the context
  that held them may be gone;
- on Stop, at the end of a turn, the `new` ones as the reason of a `"decision": "block"`, so the
  agent goes on with them instead of stopping. An agent that was working when its mail came gets
  it this way, with nothing typed into its pane. With no new message it prints nothing and the
  agent stops.

It prints nothing when there is nothing to show or the agent is not a reader. With an empty
`readers` list it returns before asking herdr for the agent's name. Each message is cut at 2,000
characters and at most the newest 10 are shown; `courier inbox` prints the rest. The hook always
exits 0, so a fault in it cannot block a turn; the fault goes to stderr.

In the settings file of the agent's Claude config dir:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "courier inbox --hook", "timeout": 10 }] }
    ],
    "SessionStart": [
      {
        "matcher": "compact|resume",
        "hooks": [{ "type": "command", "command": "courier inbox --hook", "timeout": 10 }]
      }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "courier inbox --hook", "timeout": 10 }] }
    ]
  }
}
```

Several agents can share a config dir. Only the agents in `mailbox.readers` get anything back.

### Waking an idle reader

The hooks above run only when the agent starts or ends a turn, so a message filed while it sits
idle still needs its pointer line. `courier inbox --wait [SECONDS]` is meant for a second Stop
hook that Claude Code runs in the background with `asyncRewake`: it waits up to SECONDS (default
3000) for a message filed after the turn ended, prints it on stderr, records it as shown and
exits 2, which wakes the session with the text as a system reminder. Messages already new when it
starts are left to the blocking Stop hook. Each wait writes a token to `mailbox/<name>.wait`; a
wait whose token a newer one replaced exits 0, so a reader has one waiter at a time. Every start,
wake, step-aside and time-out is a line in `hook.log`. Give the hook a timeout above SECONDS,
since Claude Code enforces it on an `asyncRewake` hook:

```json
{ "type": "command", "command": "courier inbox --wait", "asyncRewake": true, "timeout": 3600 }
```

`courier inbox NAME --wait` waits only in the pane of agent NAME and returns at once in any
other pane, so the hook can be tried on one agent of a config dir several agents share.

The wait touches `mailbox/<name>.wait` every second. While that file is under 5 seconds old,
courier serve files a new message for the reader without typing its pointer line, so the message
comes once; when the wait ends without showing it (time-out, killed, or replaced), the message
gets its pointer line at once. The idle wake and the 5 minute re-wakes stay as they are.

## Pointer lines

With `mailbox.write = true`, a message for an agent in `mailbox.readers` is typed into its pane
as one line instead of its text:

```text
[courier] 6f555376a5 from lane: in your mailbox, courier inbox --show 6f555376a5
```

Submitting that line is a prompt, so the UserPromptSubmit hook runs and adds the text from the
mailbox: the agent reads each message once, not once typed and once from the hook. Messages that
go in one prompt become one pointer line each. A slash command such as `/compact` is still typed
whole, since the agent reads it only as the whole prompt.

courier's own notices to a reader (a ghost report, a verify result, a task notice) are filed the
same way, before their pointer line is typed. A notice that cannot be filed, say on a full disk,
is typed in full instead, so a pointer line always has a message behind it.

The hook also reads the ids of the pointer lines in the prompt:

- an id already shown or done (the same message typed again, say after a ghost) gets one line
  saying so, and its text is not shown again;
- an id the mailbox does not have yet gets one line naming `courier inbox --show ID`. courier
  serve files a message just after queuing it, so the hook first waits up to 2 seconds for it.

A reader without the hook still sees the id and the command to read it.

## When a message counts as sent

For a reader the pointer line only wakes the agent; the message is the mailbox row. courier serve
marks the item `sent` when the hook has shown that row, and until then keeps it `filed`:

- a reader that is working, or whose input box holds other text (someone typing, or a line left
  there), gets no pointer line at all. The Stop hook brings the message at the end of the turn, or
  the prompt-submit hook when the text in the box is sent. The messages behind it do not wait for
  that box either.
- a pointer line that ends any other way than shown (left in the box, refused, failed) leaves the
  item `filed`, not lost.
- a message filed while its reader was working, with no pointer line typed yet, gets the pointer
  line about 10 seconds after the reader goes idle if no Stop hook has shown it by then.
- a filed message the hook has not shown after 5 minutes gets its pointer line typed again, at most
  3 times, each time through the same checks as the first.
- a filed message stays filed across a restart of courier serve.

`courier status` lists filed items, and `courier send --wait` returns when its message is sent or
filed.

## Turning it off

Set `mailbox.write = false` and restart courier serve; the files stay and nothing reads them. Remove
the hook entries, or empty `mailbox.readers`, to stop the context injection.
