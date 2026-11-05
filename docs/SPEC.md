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
| `verifying` | Submit returned; later observations check what happened. May finish sent or failed. A guarded Enter retry stays verifying. |
| `sent` | Verification finished with evidence or an explicitly unconfirmed outcome. Final. |
| `failed` | Delivery cannot continue safely, retries/hold expired, or an in-flight journal entry was recovered after restart. Final. |
| `cancelled` | Explicit cancellation before submission. Final. |

The worker is the only pane writer. Ordinary traffic is FIFO per pane; human priority, routing
and release of held traffic can affect ordering. A message waiting for a missing receiver
may have an empty pane identifier and a wall-clock hold deadline. Known missing receivers
are looked up periodically; release depends on the captured session. Changed identity normally
fails, but configured missing-receiver holds and route fallback can keep an unsent item waiting.

Observation failures can defer an item. Missing-target retries and known pre-write submit
rejections use exponential backoff and a five-attempt limit. Only `agent_blocked` and
`agent_not_ready` are treated as submit errors known to have written no bytes.

On restart, queued/waiting work is queued again (name-held work remains held). A record already
submitting/verifying with a pane becomes failed with delivery unknown; its text is never replayed.
The journal also binds pending work to a herdr server socket, preventing replay onto another server.

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
| `deliver(message)` | `submit`, guarded `enter` | Paste and submit text. Enter alone is a single recovery attempt for a matching multiline paste. |
| `confirm(message)` | `agent_get`, `read_ansi` | Observe status and input after submission; classify evidence, stuck input or an unconfirmed outcome. |
| `direction` | No method; herdr is push | Sender-side dispatcher initiates the pane write. |

`CliHerdr.submit` invokes `herdr agent prompt`; its Enter method invokes
`herdr pane send-keys ... enter`. The test carrier `PaneHerdr` instead uses
`pane send-text` followed by Enter. Transport errors retain their codes so the dispatcher
can distinguish safe pre-write rejection from an ambiguous write.

## What sent proves

After submission the verifier waits up to five seconds. Working/blocked status or a cleared
input box normally ends as sent. If the deadline passes with text still held in the input,
delivery fails. An exact multiline match takes precedence over status: after the deadline,
one Enter may be retried only with a fresh matching observation, idle/done status, no focus
or hold, and matching captured identity. It never pastes the text again. Still-matching input
after that retry fails.

Other inconclusive input at the deadline becomes sent with an `unconfirmed:` reason.
An observation error while in flight also becomes unconfirmed sent once its deadline passes.
Thus sent means the dispatcher will not submit again; it does not always prove receipt,
model acceptance, execution, completion or a reply. Mailbox read/done markers and task-ledger
completion are separate from pane delivery.

The implementation avoids a second possibly successful text submission for a retained item:
ambiguous writes fail rather than retry, and restart does not replay in-flight text.
This is not end-to-end exactly-once delivery. Pre-write retries are allowed, one Enter recovery
is allowed, and client deduplication lasts only while an item is retained. A sender using a
new identifier can send the same text again. Receiver-side rejection such as a usage limit
is not currently a distinct delivery state.

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
