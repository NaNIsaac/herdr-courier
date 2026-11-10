"""``config.toml`` in the state directory, or the file ``COURIER_CONFIG`` names, laid over the
built-in values in ``defaults``."""

import os
import re
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
    controller: str = defaults.LEDGER_CONTROLLER
    secretary: str = defaults.LEDGER_NOTIFY_TO
    resume_for: tuple[str, ...] = ()
    notify_to: str = ""
    notify_on_override: bool = defaults.LEDGER_NOTIFY_ON_OVERRIDE
    notify_on_capacity: bool = defaults.LEDGER_NOTIFY_ON_CAPACITY
    check_execution: bool = defaults.LEDGER_CHECK_EXECUTION
    membership_field: str = defaults.LEDGER_MEMBERSHIP_FIELD


@dataclass(frozen=True)
class Hold:
    when_missing: tuple[str, ...] = defaults.HOLD_WHEN_MISSING
    summary_to: dict[str, str] = field(default_factory=lambda: dict(defaults.HOLD_SUMMARY_TO))
    ttl: float = field(default=defaults.HOLD_TTL, metadata={"max": 7 * 24 * 3600})
    summary_every: float = field(default=defaults.HOLD_SUMMARY_EVERY, metadata={"max": 24 * 3600})


@dataclass(frozen=True)
class Delivery:
    merge_chars: int = field(default=defaults.DELIVERY_MERGE_CHARS, metadata={"max": 100_000})


@dataclass(frozen=True)
class Files:
    inbox: str = defaults.FILES_INBOX


@dataclass(frozen=True)
class Human:
    name: str = ""
    default_to: str = ""


@dataclass(frozen=True)
class QuestionProvider:
    command: tuple[str, ...] = ()
    timeout: float = 10.0


@dataclass(frozen=True)
class Questions:
    adapters: dict[str, QuestionProvider] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    panes: Panes = Panes()
    doctor: Doctor = Doctor()
    ovis: Ovis = Ovis()
    mailbox: Mailbox = Mailbox()
    hold: Hold = Hold()
    ledger: Ledger = Ledger()
    delivery: Delivery = Delivery()
    files: Files = Files()
    human: Human = Human()
    questions: Questions = field(default_factory=Questions)
    path: Path | None = None


SECTIONS = {
    "panes": Panes,
    "doctor": Doctor,
    "ovis": Ovis,
    "mailbox": Mailbox,
    "hold": Hold,
    "ledger": Ledger,
    "delivery": Delivery,
    "files": Files,
    "human": Human,
    "questions": Questions,
}


CONFIG_VARIABLE = "COURIER_CONFIG"


def config_path(root: Path | None = None) -> Path:
    """The file ``COURIER_CONFIG`` names, else ``config.toml`` in ``root`` (the state directory)."""
    if named := os.environ.get(CONFIG_VARIABLE):
        return Path(named).expanduser()
    return (root or state_dir()) / "config.toml"


def named_by_variable(path: Path) -> bool:
    named = os.environ.get(CONFIG_VARIABLE)
    return bool(named) and Path(named).expanduser() == path


def load(path: Path | None = None) -> Config:
    """The settings in ``path`` (default: ``config_path()``), or the defaults if it is absent.

    A file that ``COURIER_CONFIG`` names must exist: the settings it holds would otherwise be
    replaced by the defaults without a word.
    """
    path = path or config_path()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if named_by_variable(path):
            raise ConfigError(f"{path}: not found ({CONFIG_VARIABLE} names it)") from None
        return Config()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if unknown := sorted(set(data) - set(SECTIONS)):
        raise ConfigError(f"{path}: unknown section [{unknown[0]}]")
    parts = {
        name: (
            _questions(path, data.get(name, {}))
            if name == "questions"
            else _section(path, name, cls, data.get(name, {}))
        )
        for name, cls in SECTIONS.items()
    }
    return Config(**parts, path=path)


def _questions(path: Path, table: Any) -> Questions:
    if not isinstance(table, dict) or set(table) - {"adapters"}:
        raise ConfigError(f"{path}: questions must contain only an adapters table")
    adapters = table.get("adapters", {})
    if not isinstance(adapters, dict):
        raise ConfigError(f"{path}: questions.adapters must be a table")
    providers = {}
    for name, settings in adapters.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name == "local":
            raise ConfigError(f"{path}: invalid provider name {name!r}")
        provider = _section(path, f"questions.adapters.{name}", QuestionProvider, settings)
        if not provider.command or any(not arg or "\0" in arg for arg in provider.command):
            raise ConfigError(f"{path}: provider command must be a nonempty argv list")
        if provider.timeout <= 0:
            raise ConfigError(f"{path}: provider timeout must be positive")
        providers[name] = provider
    return Questions(providers)


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
            if (
                name == "ledger"
                and key in {"controller", "secretary", "membership_field"}
                and not value.strip()
            ):
                raise ConfigError(f"{path}: {name}.{key} must not be empty")
            if name == "ledger" and key == "notify_to" and value and not value.strip():
                raise ConfigError(f"{path}: {name}.{key} must be empty or a name")
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
        elif known[key].type is int:
            high = known[key].metadata["max"]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
                raise ConfigError(f"{path}: {name}.{key} must be a whole number from 0 to {high}")
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
