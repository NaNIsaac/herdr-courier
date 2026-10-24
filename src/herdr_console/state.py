"""Where hcd keeps its journal, lock and endpoint file."""

import os
import sys
from pathlib import Path


def state_dir() -> Path:
    if override := os.environ.get("HC_STATE_DIR"):
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "herdr-console"


def endpoint_path(root: Path) -> Path:
    """Where hcd tells hc its port and token."""
    return root / "endpoint.json"


def lock_path(root: Path) -> Path:
    return root / "lock"


def journal_path(root: Path) -> Path:
    return root / "journal.jsonl"
