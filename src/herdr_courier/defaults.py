"""Built-in settings: the pane labelling rule and neighbours of the workspace courier came from.

``config.toml`` in the state directory overrides any of them; ``config.example.toml`` in the
repository lists each key with these values.
"""

PANE_SEPARATOR = " · "
WORKER_SEPARATOR = " › "
# The first prefix is the one a fix suggests for an unlabelled pane.
NAMED_AFTER_TAB = ("lane", "core", "isaac")
NAMED_FREELY = ("infra",)
PATH_VARIABLES = ("DEV", "DATA")

OTHER_WRITERS = ("~/.isaac-control-center/dispatch/endpoint.json",)
OVIS_URL = "http://127.0.0.1:7317"
# Agents a person also writes to through Ovis chat, so a message queued there may be waiting.
OVIS_PANES = ("secretary",)
# Must outlast Ovis' queued send, paste to Enter to herdr showing the next turn. Measured on 400
# courier sends (2026-10-25): write to "working" seen p95 0.96 s, max 1.69 s; Ovis 0.4.14 adds
# fixed waits of 0.3 s before and 0.12 s inside its send.
OVIS_SETTLE = 3.0
# Off until a trial turns it on: courier serve then keeps a mailbox copy of every queued message.
MAILBOX_WRITE = False
# Agents whose `courier inbox --hook` puts their mailbox into the Claude context; others get nothing.
MAILBOX_READERS: tuple[str, ...] = ()
