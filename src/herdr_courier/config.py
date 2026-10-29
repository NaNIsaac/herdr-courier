"""``config.toml`` in the state directory, laid over the built-in values in ``defaults``."""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import defaults
from .state import state_dir


class ConfigError(Exception):
    """The config file exists but cannot be used; nothing falls back silently."""


@dataclass(frozen=True)
class Panes:
    separator: str = defaults.PANE_SEPARATOR
    worker_separator: str = defaults.WORKER_SEPARATOR
    named_after_tab: tuple[str, ...] = defaults.NAMED_AFTER_TAB
    named_freely: tuple[str, ...] = defaults.NAMED_FREELY
    path_variables: tuple[str, ...] = defaults.PATH_VARIABLES


@dataclass(frozen=True)
class Doctor:
    other_writers: tuple[str, ...] = defaults.OTHER_WRITERS


@dataclass(frozen=True)
class Ovis:
    url: str = defaults.OVIS_URL
    panes: tuple[str, ...] = defaults.OVIS_PANES
    settle: float = defaults.OVIS_SETTLE


@dataclass(frozen=True)
class Mailbox:
    write: bool = defaults.MAILBOX_WRITE
    readers: tuple[str, ...] = defaults.MAILBOX_READERS


@dataclass(frozen=True)
class Ledger:
    controller: str = "boss"
    secretary: str = "secretary"
    resume_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hold:
    when_missing: tuple[str, ...] = defaults.HOLD_WHEN_MISSING
    summary_to: dict[str, str] = field(default_factory=lambda: dict(defaults.HOLD_SUMMARY_TO))
    ttl: float = field(default=defaults.HOLD_TTL, metadata={"max": 7 * 24 * 3600})
    summary_every: float = field(default=defaults.HOLD_SUMMARY_EVERY, metadata={"max": 24 * 3600})


@dataclass(frozen=True)
class Config:
    panes: Panes = Panes()
    doctor: Doctor = Doctor()
    ovis: Ovis = Ovis()
    mailbox: Mailbox = Mailbox()
    hold: Hold = Hold()
    ledger: Ledger = Ledger()
    path: Path | None = None


SECTIONS = {
    "panes": Panes,
    "doctor": Doctor,
    "ovis": Ovis,
    "mailbox": Mailbox,
    "hold": Hold,
    "ledger": Ledger,
}


def config_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / "config.toml"


def load(path: Path | None = None) -> Config:
    """The settings in ``path`` (default: the state directory's), or the defaults if it is absent."""
    path = path or config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if unknown := sorted(set(data) - set(SECTIONS)):
        raise ConfigError(f"{path}: unknown section [{unknown[0]}]")
    parts = {name: _section(path, name, cls, data.get(name, {})) for name, cls in SECTIONS.items()}
    return Config(**parts, path=path)


def _section(path: Path, name: str, cls: type, table: Any):
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: {name} must be a table")
    known = {f.name: f for f in fields(cls)}
    values = {}
    for key, value in table.items():
        if key not in known:
            raise ConfigError(f"{path}: unknown key {name}.{key}")
        if known[key].type is str:
            if not isinstance(value, str):
                raise ConfigError(f"{path}: {name}.{key} must be a string")
            if key.endswith("separator") and not value:
                raise ConfigError(f"{path}: {name}.{key} must not be empty")
            if name == "ledger" and key in {"controller", "secretary"} and not value.strip():
                raise ConfigError(f"{path}: {name}.{key} must not be empty")
        elif known[key].type is bool:
            if not isinstance(value, bool):
                raise ConfigError(f"{path}: {name}.{key} must be true or false")
        elif known[key].type is float:
            high = known[key].metadata.get("max", 600)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 <= value < high
            ):
                raise ConfigError(f"{path}: {name}.{key} must be a number of seconds below {high}")
            value = float(value)
        elif known[key].type == dict[str, str]:
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) and k and v for k, v in value.items()
            ):
                raise ConfigError(f"{path}: {name}.{key} must be a table of names")
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = tuple(value)
        else:
            raise ConfigError(f"{path}: {name}.{key} must be a list of strings")
        values[key] = value
    return cls(**values)
