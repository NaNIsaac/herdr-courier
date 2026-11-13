# Message delivery specification

This describes the current implementation in `core/queue.py`, `core/gate.py` and
`core/transport.py`. It separates the sender's message from its delivery through a carrier.
Herdr is the reference carrier; implementing other carriers is outside this specification.

## Message

`Message` contains the sender's request:

| Field | Meaning |
|---|---|
| `id` | Message identifier, normally ten hexadecimal characters; a client may supply it. |
| `source` | Sender name, exposed as the source rather than a field named `from`. |
| `target` | Receiver name or pane target, exposed as the target rather than `to`. Routing can change it before submission. |
| `text` | Nonempty prompt body. |
| `human` | Human-priority request; placed ahead of queued agent traffic, behind in-flight traffic. |
| `created` | Creation time in wall-clock seconds. |
| `client_id` | Optional request deduplication key among retained items. |

Files are not fields of the queue's message record. The CLI copies attachments under the
message identifier and sends metadata to the daemon; the daemon can record a mailbox copy
with those attachments. Delivery submits the text, not a separate file transfer.

Cancellation is an operation on the identifier, not a stored message flag. It succeeds only
before submission, from `queued` or `waiting`; unknown, final and in-flight items are refused.
Cancellation ends delivery as `cancelled`; it cannot undo a paste or remove a mailbox copy.

## Delivery

`Delivery` contains the receiver binding (`pane_id`, `session`, `terminal`), state, reason,
attempt count, update time, retry time, verification deadline, held deadline and routing origin.
`Item` combines `Message` and `Delivery` for the current one-message/one-delivery implementation;
it does not create an independent delivery identifier or a multi-carrier fan-out.
The existing flat journal and API records retain their field names and order through
`Item.as_dict()`. There is no journal migration.

| State | Entry and exit |
|---|---|
| `queued` | Accepted for a resolved receiver, or restored before any possible write. May wait, submit, fail or be cancelled. |
| `waiting` | Gate refuses temporarily, observation fails, receiver is held/missing, or a retryable submit was rejected before writing. Rechecked on worker ticks; may submit, fail or be cancelled. |
| `submitting` | Journaled immediately before calling the carrier. Cancellation is now refused. Success enters verifying; a known pre-write rejection may return to waiting; other errors fail. |
| `verifying` | Submit returned; later observations check what happened. May finish sent, rejected, ghost or failed. A guarded Enter retry stays verifying. |
| `sent` | Verification finished with evidence or an explicitly unconfirmed outcome. Final. |
| `failed` | Delivery cannot continue safely, retries/hold expired, or an in-flight journal entry was recovered after restart. Final. |
| `cancelled` | Explicit cancellation before submission. Final. |
| `rejected` | A fresh receiver usage-limit response was observed after submission. Final; never automatically replayed. |
| `ghost` | Courier text remains after one Enter retry, or an exact/safe retry cannot be proved. Final; text is left in place. |
| `filed` | A mailbox reader's message that its hook has not shown yet: the pointer line was typed (however that ended), or the reader was working or had other text in its box. Not in the pane queue. Becomes `sent` once the hook shows it; may have its pointer typed again. |

The worker is the only pane writer. Ordinary traffic is FIFO per pane; human priority, routing
and release of held traffic can affect ordering. One write can carry several messages: when the
head passes the gate, the items queued right behind it that share its human flag, session and
terminal, are not backing off, are not slash commands and keep the text within `merge_chars`
are journalled `submitting` with it and submitted as one text, each under a
`--- <id> from <source> at HH:MM ---` line. The first item that does not qualify ends the run, so
order is kept, and a slash command at the head goes alone. Verification observes that merged
text; the riders take the head's final state, and a known pre-write rejection returns them to
`waiting`. A ghost head keeps its rider list in the journal, so a guarded manual submit after a
restart ends them too. A message waiting for a missing receiver
may have an empty pane identifier and a wall-clock hold deadline. Known missing receivers
are looked up periodically; release depends on the captured session. Changed identity normally
fails, but configured missing-receiver holds and route fallback can keep an unsent item waiting.

Observation failures can defer an item. Missing-target retries and known pre-write submit
rejections use exponential backoff and a five-attempt limit. Only `agent_blocked` and
`agent_not_ready` are treated as submit errors known to have written no bytes.

On restart, queued/waiting work is queued again (name-held work remains held). A record already
submitting/verifying with a pane becomes failed with delivery unknown; its text is never replayed.
A filed message, or a submitting/verifying pointer whose message the reader has not been shown,
stays `filed`.
The journal also binds pending work to a herdr server socket, preventing replay onto another server.
Recent ghost/rejected records are retained up to the normal finished-item limit without
requeuing, preserving guarded recovery/status across restarts.

## Receiver observation and safety

`Observation` is a snapshot for the gate, not proof that the receiver accepted or completed work.

| Value | Safety meaning |
|---|---|
| `status` | Only idle, done and working are sendable. Working can queue another prompt. Blocked or unknown waits before submission. |
| `session` | Must match the captured session. A missing session waits for a resume; another session is a different receiver. |
| `focused` | A hint that a person may type; increases empty-input debounce from one to three seconds. It is not a lock. |
| `line` | Classified input line: empty, typed or unknown, with evidence. Typed and unknown both wait. |
| `name` | Identifies receiver-specific between-turn settings and missing-receiver tracking. |
| `state_seq` | Status-change sequence; paired with status to detect changes between observations and restart settling time. |
| `terminal` | Captured terminal identity protects a sessionless receiver from pane reuse; a changed terminal fails that binding. |
| `rejection` | Recognized receiver error evidence, compared with the pre-submit view so an unchanged old limit banner is not a fresh refusal. |

A pane hold also blocks sending. Configured between-turn settling refuses working and requires
the status/sequence pair to remain unchanged for the configured interval. The dispatcher checks
the gate again immediately before writing. This narrows races with human input; it cannot
atomically lock a human's input across the observation and paste.

## Carrier interface

These are conceptual operations, not additional Python methods introduced by this change.
The current `Herdr` protocol provides six lower-level methods:

| Abstract operation | Current protocol methods | Reference behavior |
|---|---|---|
| `reach(to)` | `agent_get`, `agent_list`, `pane_list` | Resolve a receiver and its identity; enumerate receivers and panes for lookup/status. |
| `ready(to)` | `agent_get`, `read_ansi` | Build an observation; apply the input, identity, hold, debounce and settling gate. |
| `deliver(message)` | `submit`, guarded `enter` | Paste and submit text. Enter alone is a single recovery attempt for a matching single/multiline paste. |
| `confirm(message)` | `agent_get`, `read_ansi` | Observe status and input after submission; classify evidence, stuck input or an unconfirmed outcome. |
| `direction` | No method; herdr is push | Sender-side dispatcher initiates the pane write. |

`CliHerdr.submit` invokes `herdr agent prompt`; its Enter method invokes
`herdr pane send-keys ... enter`. The test carrier `PaneHerdr` instead uses
`pane send-text` followed by Enter. Transport errors retain their codes so the dispatcher
can distinguish safe pre-write rejection from an ambiguous write.

## What sent proves

After submission the verifier waits up to five seconds. A fresh Codex usage-limit response
ends as rejected, even when agent status still says working. An unchanged pre-submit error
is not fresh evidence; error text in the input or before a newer transcript prompt is ignored.
Working/blocked status or a cleared input normally ends as sent. Owned text takes precedence
over status. An exact single/multiline match can recover after the deadline:
one Enter may be retried only with a fresh matching observation, idle/done status, no focus
or hold, and matching captured identity. It never pastes the text again. Still-matching input
after that retry is ghost. Human edits sharing the original prefix also become ghost without
an Enter attempt. Ambiguous key transport/journal errors remain failed.

Other inconclusive input at the deadline becomes sent with an `unconfirmed:` reason.
An observation error while in flight also becomes unconfirmed sent once its deadline passes.
Thus sent means the dispatcher will not submit again; it does not always prove receipt,
model acceptance, execution, completion or a reply. Mailbox read/done markers and task-ledger
completion are separate from pane delivery.

The implementation avoids a second possibly successful text submission for a retained item:
ambiguous writes fail rather than retry, and restart does not replay in-flight text.
This is not end-to-end exactly-once delivery. Pre-write retries are allowed, one Enter recovery
is allowed, and client deduplication lasts only while an item is retained. A sender using a
new identifier can send the same text again. Rejected/ghost are carrier outcomes; mailbox
read/done and task completion are still independent.

## Guarded submit recovery

`courier submit TARGET` requests Enter only, never arbitrary text or another paste.
The HTTP handler schedules the request; the existing dispatcher worker remains the only
pane writer. Concurrent pending requests for one pane share the same result.

The worker compares the current input with complete retained courier message text, including
renderer continuation indentation. Shared prefixes/substrings do not prove ownership.
It re-reads immediately before Enter and refuses edits, unknown/dialog input, focus, holds,
working/blocked receivers, visible usage refusal and changed/missing captured identity.
Cancelled/rejected records cannot authorize a key. Recent sent records can authorize explicit
recovery of an older dispatcher's false-positive sent outcome, only with the same exact guards.

The Enter intent and retry marker are journaled before the key. Recovery moves the record
to the head in verifying state; its cleared input is checked normally. A crash never replays
that key or message. CLI success may mean verifying, not proven receipt; inspect status.
There is no force/raw-input fallback. A timed-out request may still be pending.

Unknown input gets one larger read capped at 256 lines. A dim, exact Codex placeholder with
its shortcuts footer can prove empty input when padding is missing. Known queued multiline
text can prove typed input without a background; it never proves empty. Dialog choices and
unrecognized layouts stay unknown. Very long/truncated or differently wrapped text may
remain unrecoverable rather than permit an unproved key.

Upgrade both client and serve before using the new outcomes/command. A 0.2.0 serve process
has only the older multiline recovery and no submit endpoint; changing installed files does
not update an already-running process. No deployment/restart is performed by this change.

## Setup policy configuration

The state directory's `config.toml` controls setup policy: `ledger.controller` (default
`boss`), `ledger.notify_to` (empty inherits the existing `ledger.secretary` setting,
default `secretary`), `ledger.notify_on_override`, `ledger.notify_on_capacity` and
`ledger.check_execution` (all three default true). The notice receiver also has route-change
permission. Existing configurations and journal notification fields remain valid.
Disabling notices prevents the corresponding mailbox and pane notifications; existing pending
notices remain stored for when notifications are enabled again. An existing pending override
notice still blocks another reassignment until sent. Disabling the execution check
removes the requirement for `lane_state_task` and `descendants_working` proof during reassignment;
the receiver's working status, evidence, identity and in-flight checks still apply.

Other setup choices already live in `panes.*` (label conventions), `doctor.other_writers`
(competing writers), `ovis.*` (between-turn receivers), `hold.*` (missing receivers and
summary recipients), and `mailbox.readers`. Optional membership checks use the existing
`COURIER_ROLES_FILE` environment variable and `ledger.membership_field` (default `lanes`);
without that environment variable no membership list is assumed.
An ownership field or input-ownership check is a safety concept, independent of agent names.
