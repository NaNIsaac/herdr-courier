# Human questions

The courier distribution installs both `pepperpaw` and `ppw`. They use the same config.toml
and state root as courier. This interface reads questions from configured providers; it does
not create native reply requests, human mailboxes or a question database.

Configure a human identity and a provider:

```toml
[human]
name = "alex"
default_to = "reviewer"

[questions.adapters.example]
command = ["question-provider", "--profile", "personal"]
timeout = 10.0
```

The example command must be supplied separately. No provider is enabled by default.
Provider names contain ASCII letters, digits, underscores or hyphens; local is reserved.
Commands are argv arrays, never shell strings. Environment variables in the configured argv
are expanded before execution. Answer text is sent only on stdin and is never interpolated
into a command. Configured commands run with the current user's permissions.

## Commands

| Command | Behavior |
|---|---|
| `ppw q` | List open questions addressed to human.name, with provider-prefixed IDs. |
| `ppw q --all` | Include questions that are not open. |
| `ppw q example:a17` | Show full text, sender, recipient, state, and optional links/options. |
| `ppw a example:a17 "answer"` | Re-fetch the question, check state/revision, then answer it once. |
| `ppw answer example:a17 --file answer.txt` | Alias; read a UTF-8 answer, normalizing CRLF to LF. |
| `ppw send "message" [--to NAME] [--file PATH]` | Use the configured human identity and courier's human-priority delivery. |

All commands accept --json and --width; width applies to plain question output. Full question
text wraps without truncation, including Korean text. Exact IDs are required; the part after
the first colon is opaque and may itself contain colons. Two providers can return the same
local ID without a collision. Merely listing or viewing does not acknowledge or answer.

No configured provider is distinct from an empty question list (exit 3). Failed providers
remain visible while successful providers' items are listed (exit 1). JSON listing returns
configured, principal, items and errors. Other validation/provider failures exit 1.
Send retains courier's delivery failures and queue receipt. No automatic provider write retry
is made; after an uncertain answer, get/reconcile with the provider before retrying.
An answer is 1..20,000 characters; attachment limits are courier's existing limits.

## Provider protocol

The CLI invokes `<command> list|get|answer` with one UTF-8 JSON object on stdin, waits for
one JSON object on stdout, and treats nonzero exit as failure. Diagnostics belong on stderr.
The request limit is 128 KiB, stdout 1 MiB and stderr 64 KiB. Output readers stop and terminate
the provider when a limit is exceeded. Timeout defaults to 10 seconds and must be positive
and below 600 seconds. Only the directly launched process is managed; providers must finish
their children and close inherited output handles.

| Operation | Input | Output |
|---|---|---|
| list | {principal} | {items: [item, ...]} |
| get | {principal, id} | item, including its current revision |
| answer | {principal, id, text, request_id, revision} | {state, receipt, notification} |

An item has nonempty string id/from/to/text/state and a finite numeric created Unix timestamp.
Its to must match principal. A get must return the requested id. Duplicate IDs within one
list are rejected. Links/options and other JSON fields are preserved for full display.
The CLI treats state=open as answerable and requires a nonempty string revision before
answering. It gets the item again immediately before the write and refuses a changed revision
or state. The provider must atomically compare the supplied revision when applying the answer:
the client-side recheck alone cannot prevent a concurrent answer.

Answer receipts require nonempty string state/receipt/notification. The provider owns
idempotency for request_id, its source of truth, and any sender notification. A timeout or
malformed receipt may follow a successful write; the CLI reports failure/uncertainty and never
blindly re-sends. A repeated CLI answer re-gets the authoritative state and refuses a question
that is no longer open. The CLI does not send a second independent reply to an agent.

Do not parse a provider's formatted terminal output. Adapt the source's typed records into
this protocol. Source-specific numbering, approvals, cards, network access and execution
policies belong in the provider, not in courier.
