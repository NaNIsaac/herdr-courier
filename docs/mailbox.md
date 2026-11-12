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

The hook also reads the ids of the pointer lines in the prompt:

- an id already shown or done (the same message typed again, say after a ghost) gets one line
  saying so, and its text is not shown again;
- an id the mailbox does not have yet gets one line naming `courier inbox --show ID`. courier
  serve files a message just after queuing it, so the hook first waits up to 2 seconds for it.

A reader without the hook still sees the id and the command to read it.

## Turning it off

Set `mailbox.write = false` and restart courier serve; the files stay and nothing reads them. Remove
the hook entries, or empty `mailbox.readers`, to stop the context injection.
