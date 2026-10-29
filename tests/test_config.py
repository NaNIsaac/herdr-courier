import tomllib
from pathlib import Path

import pytest

from herdr_courier import cli, config, defaults, panes, state

EXAMPLE = Path(__file__).parents[1] / "config.example.toml"


def test_no_file_means_defaults(state_dir):
    loaded = config.load()
    assert loaded.path is None
    assert loaded.panes.named_after_tab == defaults.NAMED_AFTER_TAB
    assert loaded.ovis == config.Ovis(defaults.OVIS_URL, defaults.OVIS_PANES, defaults.OVIS_SETTLE)


def test_file_overrides_only_what_it_sets(state_dir):
    (state_dir / "config.toml").write_text(
        '[panes]\nnamed_after_tab = ["agent"]\nseparator = " | "\n', encoding="utf-8"
    )
    loaded = config.load()
    assert loaded.path == state_dir / "config.toml"
    assert loaded.panes.named_after_tab == ("agent",)
    assert loaded.panes.separator == " | "
    assert loaded.panes.named_freely == defaults.NAMED_FREELY
    assert loaded.doctor == config.Doctor()


def test_settle_takes_whole_seconds(state_dir):
    (state_dir / "config.toml").write_text(
        '[ovis]\npanes = ["boss"]\nsettle = 5\n', encoding="utf-8"
    )
    assert config.load().ovis == config.Ovis(panes=("boss",), settle=5.0)


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("[panes\n", "Expected"),
        ("[pane]\n", "unknown section [pane]"),
        ("[panes]\nprefixes = []\n", "unknown key panes.prefixes"),
        ("[panes]\nseparator = 1\n", "panes.separator must be a string"),
        ('[panes]\nnamed_freely = "infra"\n', "panes.named_freely must be a list of strings"),
        ('[panes]\nseparator = ""\n', "panes.separator must not be empty"),
        ("panes = 1\n", "panes must be a table"),
        ('[ovis]\nsettle = "3"\n', "ovis.settle must be a number of seconds"),
        ("[ovis]\nsettle = nan\n", "ovis.settle must be a number of seconds"),
        ("[ovis]\nsettle = true\n", "ovis.settle must be a number of seconds"),
        ("[hold]\nttl = 604800\n", "hold.ttl must be a number of seconds below 604800"),
        ('[hold]\nsummary_to = ["boss"]\n', "hold.summary_to must be a table of names"),
        ('[hold]\nsummary_to = { boss = "" }\n', "hold.summary_to must be a table of names"),
    ],
)
def test_bad_file_is_an_error(state_dir, text, expect):
    (state_dir / "config.toml").write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert expect in str(caught.value)


def test_hold_takes_hours_and_its_own_pairs(state_dir):
    (state_dir / "config.toml").write_text(
        '[hold]\nwhen_missing = ["lead"]\nsummary_to = { lead = "deputy" }\nttl = 7200\n',
        encoding="utf-8",
    )
    hold = config.load().hold
    assert hold == config.Hold(when_missing=("lead",), summary_to={"lead": "deputy"}, ttl=7200.0)


def test_example_matches_the_defaults():
    data = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    built_in = config.Config()
    for section, values in data.items():
        for key, value in values.items():
            expected = getattr(getattr(built_in, section), key)
            assert (tuple(value) if isinstance(value, list) else value) == expected, key
    assert config.load(EXAMPLE).panes == built_in.panes


def test_custom_rule():
    rule = config.Panes(named_after_tab=("agent",), named_freely=(), separator=": ")
    ok = panes.Pane("w1:p1", "agent: build", "", None, False)
    assert panes.label_problem(ok, "build", rule) is None
    old = panes.Pane("w1:p1", "lane · build", "", None, False)
    assert "does not start with agent and :" in panes.label_problem(old, "build", rule)


def test_rule_without_prefixes_checks_nothing():
    rule = config.Panes(named_after_tab=(), named_freely=())
    assert panes.label_problem(panes.Pane("w1:p1", "", "", None, False), "x", rule) is None


def test_path_variables(monkeypatch):
    monkeypatch.setenv("WORK", "C:\\work")
    assert panes.short_path("C:\\work\\a", ("WORK",)) == "$WORK\\a"
    assert panes.short_path("C:\\work\\a", ()) == "C:\\work\\a"


def test_cli_panes_refuses_a_broken_config(state_dir, capsys):
    (state_dir / "config.toml").write_text("[panes]\nbogus = 1\n", encoding="utf-8")
    assert cli.main(["panes"]) == cli.EXIT_FAILED
    assert "unknown key panes.bogus" in capsys.readouterr().err


def test_old_state_directory_moves_once(monkeypatch, tmp_path):
    monkeypatch.delenv("COURIER_STATE_DIR")
    monkeypatch.setattr(state, "base_dir", lambda: tmp_path)
    old = tmp_path / state.OLD_NAME
    old.mkdir()
    (old / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    assert state.adopt_old_state() == old
    assert (tmp_path / state.NAME / "journal.jsonl").exists()
    assert not old.exists()
    old.mkdir()
    assert state.adopt_old_state() is None
    assert old.exists()


def test_old_state_directory_ignored_with_override(state_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(state, "base_dir", lambda: tmp_path)
    (tmp_path / state.OLD_NAME).mkdir()
    assert state.adopt_old_state() is None
