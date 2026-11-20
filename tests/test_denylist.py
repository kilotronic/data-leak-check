# Copyright (c) 2026 Kilotronic LLC.
# SPDX-License-Identifier: MIT
"""Reading the denylist: terms, regexes, comments, separators, and where the list comes from."""

from __future__ import annotations

from pathlib import Path

import pytest

from data_leak_check.check import DenylistError, load_denylist, matches, parse_denylist


def terms(text: str) -> list[str]:
    return [e.source for e in parse_denylist(text)]


def test_newlines_commas_comments_and_blank_lines() -> None:
    text = """
    # a comment line
    acme-corp, globex

      initech
    # another, with a comma
    """
    assert terms(text) == ["acme-corp", "globex", "initech"]


def test_entries_are_numbered_from_one_in_order() -> None:
    entries = parse_denylist("alpha, beta\ngamma")
    assert [(e.number, e.source) for e in entries] == [(1, "alpha"), (2, "beta"), (3, "gamma")]


def test_a_regex_runs_to_the_end_of_its_line_so_it_may_hold_commas() -> None:
    assert terms(r"alpha, re:[a-z0-9]{20,}\.example\.net, beta") == [
        "alpha",
        r"re:[a-z0-9]{20,}\.example\.net, beta",
    ]


def test_plain_terms_match_case_insensitively_anywhere() -> None:
    entries = parse_denylist("Acme Corp")
    assert [m for _, m in matches("we work at ACME CORP now", entries)] == ["ACME CORP"]
    assert matches("acmecorp", entries) == []


def test_plain_terms_are_literal_not_regex() -> None:
    entries = parse_denylist("a.b")
    assert matches("axb", entries) == []
    assert matches("see a.b here", entries)


def test_regex_entries_match_case_insensitively() -> None:
    entries = parse_denylist(r"re:\bglobex\b")
    assert matches("Globex rules", entries)
    assert matches("GLOBEX", entries)
    assert matches("globexample", entries) == []


def test_regex_entry_for_hostnames() -> None:
    entries = parse_denylist(r"re:[a-z0-9]{20,}\.bx\.internal\.example\.net")
    assert matches("vm.abcdefghij0123456789xy.bx.internal.example.net", entries)
    assert matches("vm.short.bx.internal.example.net", entries) == []


def test_each_match_names_its_entry() -> None:
    entries = parse_denylist("alpha\nbeta\nre:gam+a")
    found = matches("beta and gammma", entries)
    assert [(e.number, m) for e, m in found] == [(2, "beta"), (3, "gammma")]


def test_a_bad_regex_is_refused_without_echoing_it() -> None:
    with pytest.raises(DenylistError) as err:
        parse_denylist("alpha\nre:secret-(unclosed")
    assert "entry 2" in str(err.value)
    assert "secret" not in str(err.value)


@pytest.mark.parametrize("text", ["re:", "re:   ", "re:x*", "re:(|y)"])
def test_regexes_that_match_empty_text_are_refused(text: str) -> None:
    with pytest.raises(DenylistError, match="entry 1"):
        parse_denylist(text)


def test_only_comments_is_an_empty_list() -> None:
    assert parse_denylist("# nothing here\n\n  # still nothing\n") == []


# --- Where the hook's list comes from


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_flag_file_comes_first(tmp_path: Path) -> None:
    f = write(tmp_path / "list", "from-flag")
    env = {"DATA_LEAK_DENYLIST": "from-env", "HOME": str(tmp_path)}
    entries, source = load_denylist(str(f), env)
    assert [e.source for e in entries] == ["from-flag"]
    assert source == f"--denylist-file {f}"


def test_inline_env_comes_before_the_env_file(tmp_path: Path) -> None:
    f = write(tmp_path / "list", "from-env-file")
    env = {"DATA_LEAK_DENYLIST": "a, b", "DATA_LEAK_DENYLIST_FILE": str(f), "HOME": str(tmp_path)}
    entries, source = load_denylist(None, env)
    assert [e.source for e in entries] == ["a", "b"]
    assert source == "$DATA_LEAK_DENYLIST"


def test_env_file_comes_before_the_default_file(tmp_path: Path) -> None:
    f = write(tmp_path / "list", "from-env-file")
    write(tmp_path / ".config" / "data-leak-check" / "denylist", "from-default")
    env = {"DATA_LEAK_DENYLIST_FILE": str(f), "HOME": str(tmp_path)}
    entries, _ = load_denylist(None, env)
    assert [e.source for e in entries] == ["from-env-file"]


def test_default_file_under_home(tmp_path: Path) -> None:
    write(tmp_path / ".config" / "data-leak-check" / "denylist", "from-default")
    entries, source = load_denylist(None, {"HOME": str(tmp_path)})
    assert [e.source for e in entries] == ["from-default"]
    assert source.endswith("data-leak-check/denylist")


def test_default_file_follows_xdg_config_home(tmp_path: Path) -> None:
    write(tmp_path / "xdg" / "data-leak-check" / "denylist", "from-xdg")
    env = {"HOME": str(tmp_path / "nowhere"), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    entries, _ = load_denylist(None, env)
    assert [e.source for e in entries] == ["from-xdg"]


def test_empty_inline_env_falls_through(tmp_path: Path) -> None:
    write(tmp_path / ".config" / "data-leak-check" / "denylist", "from-default")
    entries, _ = load_denylist(None, {"DATA_LEAK_DENYLIST": "  \n", "HOME": str(tmp_path)})
    assert [e.source for e in entries] == ["from-default"]


def test_nothing_configured_is_empty(tmp_path: Path) -> None:
    entries, source = load_denylist(None, {"HOME": str(tmp_path)})
    assert entries == []
    assert source is None


def test_a_missing_flag_file_is_empty_not_an_error(tmp_path: Path) -> None:
    entries, source = load_denylist(str(tmp_path / "absent"), {"HOME": str(tmp_path)})
    assert entries == []
    assert source is None
