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
