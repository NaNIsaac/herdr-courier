"""Where courierd keeps its journal, lock and endpoint file, and courier its config."""

import os
import sys
from pathlib import Path

NAME = "herdr-courier"
# Releases before the rename (0.1, herdr-console) kept the same files here.
OLD_NAME = "herdr-console"


def base_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def state_dir() -> Path:
    if override := os.environ.get("COURIER_STATE_DIR"):
        return Path(override)
    return base_dir() / NAME


def adopt_old_state() -> Path | None:
    """Move a 0.1 state directory to the new name when only the old one exists.

    Returns the old path when it moved. Raises OSError when it cannot, as when a 0.1 hcd still
    holds its lock file open.
    """
    if os.environ.get("COURIER_STATE_DIR"):
        return None
    old, new = base_dir() / OLD_NAME, base_dir() / NAME
    if new.exists() or not old.is_dir():
        return None
    old.rename(new)
    return old


def endpoint_path(root: Path) -> Path:
    """Where courierd tells courier its port and token."""
    return root / "endpoint.json"


def lock_path(root: Path) -> Path:
    return root / "lock"


def journal_path(root: Path) -> Path:
    return root / "journal.jsonl"
