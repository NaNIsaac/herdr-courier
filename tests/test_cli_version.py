"""Version flags must work before any subcommand or server startup."""
from __future__ import annotations

import pytest

from herdr_courier import cli


@pytest.mark.parametrize("entrypoint", [cli.main, cli.serve_main])
def test_version_flag_is_available_without_subcommands(monkeypatch, capsys, entrypoint):
    monkeypatch.setattr(cli, "_distribution_version", lambda: "9.8.7")
    with pytest.raises(SystemExit) as exc:
        entrypoint(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "herdr-courier 9.8.7"


def test_missing_package_metadata_is_explicit(monkeypatch):
    def fail(_):
        raise cli.PackageNotFoundError("herdr-courier")

    monkeypatch.setattr(cli, "version", fail)
    assert "not installed" in cli._distribution_version()
