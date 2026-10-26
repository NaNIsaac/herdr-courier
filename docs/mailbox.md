# Mailbox

courier types each message into the target's pane. The journal then says whether the text was
typed, but not whether the agent saw it, and a message typed while the agent compacts or restarts
can be lost from its context. The mailbox keeps a second copy of each message outside the agent's
context until the agent marks it done. It is off by default, and typing into panes does not change
when it is on.

## Files

With `mailbox.write = true` in `config.toml`, courier serve appends to
`<state dir>/mailbox/<target>.jsonl` each time it queues a message. The target is lowercased, and
characters other than letters, digits, `.`, `_` and `-` become `_`, so `w3:pC` is `w3_pc.jsonl`.
A message sent to a pane id is filed under that id, not under the agent's name.

Each line is one JSON record:

| kind | written by | fields |
| --- | --- | --- |
| `message` | courier serve, when it queues the item | `id` (the journal's), `to`, `from`, `created`, `human`, `text` |
| `shown` | `courier inbox --hook`, when it puts the message into the agent's context | `id`, `at`, `event` |
| `done` | `courier inbox --ack ID` | `id`, `at` |

A message is `new` until it is shown, then `read`, then `done`. A client retry that returns the
same queued item adds no second `message` line. A line cut short by a crash is skipped.

`mailbox/hook.log` has one line per hook call, including calls with nothing to show: time, agent,
event, the ids shown (`-` for none) and the bytes of context added.

## Reading

```sh
courier inbox            # this pane's agent: every message not done, in full
courier inbox boss --all # another agent's, done ones included
courier inbox --show ID  # one message in full
courier inbox --ack ID   # mark done
```

Without a name, `courier inbox` uses the herdr agent name of the calling pane (`HERDR_PANE_ID`
matched in `herdr agent list`).

## Claude Code hook

`courier inbox --hook` reads the hook payload on stdin. For an agent listed in `mailbox.readers`, it
prints the open messages as `additionalContext` and records them as shown:

- on UserPromptSubmit, the `new` ones;
- on SessionStart (after a compaction, resume or restart), every one not `done`, since the context
  that held them may be gone.

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
    ]
  }
}
```

Several agents can share a config dir. Only the agents in `mailbox.readers` get anything back.

## Turning it off

Set `mailbox.write = false` and restart courier serve; the files stay and nothing reads them. Remove
the hook entries, or empty `mailbox.readers`, to stop the context injection.
