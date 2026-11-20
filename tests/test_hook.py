# Copyright (c) 2026 Kilotronic LLC.
# SPDX-License-Identifier: MIT
"""The hook's three stages and the commit-range mode, run end to end in throwaway git repositories."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import Repo

DENYLIST = "acme-corp\nGlobex\nre:\\bwidget-\\d+\\b\nre:[a-z0-9]{20,}\\.bx\\.internal\\.example\\.net"


@pytest.fixture
def listed(repo: Repo, home: Path) -> Repo:
    """A repository whose default denylist file (under HOME, outside the repo) holds DENYLIST."""
    f = home / ".config" / "data-leak-check" / "denylist"
    f.parent.mkdir(parents=True)
    f.write_text(DENYLIST)
    repo.commit("start", {"README.md": "hello\n"})
    return repo


# --- pre-commit: the staged changes


def test_staged_clean_change_passes(listed: Repo) -> None:
    listed.write("notes.txt", "nothing to see\n")
    listed.git("add", "notes.txt")
    result = listed.check("--staged")
    assert result.returncode == 0, result.stderr


def test_staged_added_line_is_refused_with_file_and_line(listed: Repo) -> None:
    listed.write("notes.txt", "line one\nline two\nwe ship for ACME-CORP\n")
    listed.git("add", "notes.txt")
    result = listed.check("--staged")
    assert result.returncode == 1
    assert "notes.txt:3: added line matches entry 1" in result.stderr


def test_line_numbers_follow_the_new_file_in_a_modified_file(listed: Repo) -> None:
    listed.write("README.md", "hello\nmore\nwidget-42 here\n")
    listed.git("add", "README.md")
    result = listed.check("--staged")
    assert "README.md:3: added line matches entry 3" in result.stderr


def test_removed_lines_are_not_refused(listed: Repo) -> None:
    listed.commit("old", {"old.txt": "globex\n"})
    listed.write("old.txt", "clean\n")
    listed.git("add", "old.txt")
    assert listed.check("--staged").returncode == 0


def test_staged_is_the_default_mode(listed: Repo) -> None:
    listed.write("a.txt", "Globex\n")
    listed.git("add", "a.txt")
    assert listed.check().returncode == 1


def test_unstaged_changes_are_not_checked(listed: Repo) -> None:
    listed.write("a.txt", "Globex\n")
    assert listed.check("--staged").returncode == 0


def test_a_new_file_path_is_checked_and_not_echoed(listed: Repo) -> None:
    listed.write("docs/acme-corp-plan.md", "clean text\n")
    listed.git("add", "-A")
    result = listed.check("--staged")
    assert result.returncode == 1
    assert "file path matches entry 1" in result.stderr
    assert "acme" not in result.stderr.lower()


def test_lines_in_a_file_whose_path_matches_do_not_echo_the_path(listed: Repo) -> None:
    listed.write("globex.txt", "Globex\n")
    listed.git("add", "-A")
    result = listed.check("--staged")
    assert result.returncode == 1
    assert "globex" not in result.stderr.lower()


def test_a_renamed_file_is_checked_by_its_new_path(listed: Repo) -> None:
    listed.git("mv", "README.md", "widget-7.md")
    result = listed.check("--staged")
    assert result.returncode == 1
    assert "file path matches entry 3" in result.stderr


def test_staged_check_in_a_repository_with_no_commits(repo: Repo, home: Path) -> None:
    repo.write("a.txt", "fine\nglobex\n")
    repo.git("add", "a.txt")
    result = repo.check("--staged", env={"DATA_LEAK_DENYLIST": "globex"})
    assert result.returncode == 1
    assert "a.txt:2: added line matches entry 1" in result.stderr


def test_staged_check_also_judges_the_identity_git_will_stamp(listed: Repo) -> None:
    listed.write("a.txt", "fine\n")
    listed.git("add", "a.txt")
    result = listed.check("--staged", env={"GIT_AUTHOR_EMAIL": "ada@globex.example.com"})
    assert result.returncode == 1
    assert "author email matches entry 2" in result.stderr
    assert "globex" not in result.stderr.lower()


# --- Redaction and --show-matches


def test_terms_and_matched_text_are_never_printed_by_default(listed: Repo) -> None:
    listed.write("a.txt", "acme-corp globex widget-9\n")
    listed.git("add", "a.txt")
    result = listed.check("--staged")
    out = (result.stdout + result.stderr).lower()
    assert result.returncode == 1
    assert "matches entries 1, 2, 3" in out
    for term in ("acme", "globex", "widget"):
        assert term not in out


def test_show_matches_prints_the_term_and_the_match(listed: Repo) -> None:
    listed.write("a.txt", "ACME-CORP\n")
    listed.git("add", "a.txt")
    result = listed.check("--staged", "--show-matches")
    assert result.returncode == 1
    assert "'acme-corp'" in result.stderr
    assert "'ACME-CORP'" in result.stderr


# --- commit-msg: the message file git passes


def test_commit_message_is_refused_by_line(listed: Repo) -> None:
    msg = listed.path / ".git" / "COMMIT_EDITMSG"
    msg.write_text("Fix the build\n\nAsked for by the Globex team\n")
    result = listed.check("--commit-msg", str(msg))
    assert result.returncode == 1
    assert "commit message line 3 matches entry 2" in result.stderr
    assert "globex" not in result.stderr.lower()


def test_commit_message_comments_and_the_diff_below_the_scissors_are_skipped(listed: Repo) -> None:
    msg = listed.path / ".git" / "COMMIT_EDITMSG"
    msg.write_text(
        "Fix the build\n"
        "# Please enter the commit message. globex\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/x b/x\n+acme-corp\n"
    )
    assert listed.check("--commit-msg", str(msg)).returncode == 0


def test_clean_commit_message_passes(listed: Repo) -> None:
    msg = listed.path / ".git" / "COMMIT_EDITMSG"
    msg.write_text("Fix the build\n")
    assert listed.check("--commit-msg", str(msg)).returncode == 0


# --- Commit ranges (the Action, and pre-push): messages, authors, committers, content


def test_range_checks_author_names_emails_and_committers(listed: Repo) -> None:
    base = listed.git("rev-parse", "HEAD")
    listed.commit("one", {"a.txt": "a\n"}, name="Globex Bot")
    listed.commit("two", {"b.txt": "b\n"}, email="bot@acme-corp.example.com")
    listed.git(
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "three",
        env={"GIT_COMMITTER_EMAIL": "ci@globex.example.org"},
    )
    result = listed.check("--commits", f"{base}..HEAD")
    assert result.returncode == 1
    err = result.stderr
    assert "author name matches entry 2" in err
    assert "author email matches entry 1" in err
    assert "committer name matches entry 2" in err  # commit one: same person committed
    assert "committer email matches entry 2" in err  # commit three
    assert "acme" not in err.lower() and "globex" not in err.lower()


def test_range_checks_messages_and_each_commits_added_lines(listed: Repo) -> None:
    base = listed.git("rev-parse", "HEAD")
    sha = listed.commit("Add the widget-12 notes", {"n.txt": "one\nfor acme-corp\n"})
    listed.commit("Remove it again", {"n.txt": "one\n"})
    result = listed.check("--commits", f"{base}..HEAD")
    assert result.returncode == 1
    assert f"commit {sha[:12]}: message line 1 matches entry 3" in result.stderr
    # Content added and then removed inside the range still reaches the public history.
    assert f"commit {sha[:12]}: n.txt:2: added line matches entry 1" in result.stderr


def test_range_of_clean_commits_passes(listed: Repo) -> None:
    base = listed.git("rev-parse", "HEAD")
    listed.commit("clean", {"c.txt": "fine\n"})
    result = listed.check("--commits", f"{base}..HEAD")
    assert result.returncode == 0, result.stderr
    assert "1 commit(s)" in result.stdout


def test_a_bad_range_is_an_error(listed: Repo) -> None:
    result = listed.check("--commits", "no-such-ref..HEAD")
    assert result.returncode == 2


def test_merge_commit_messages_are_checked(listed: Repo) -> None:
    base = listed.git("rev-parse", "HEAD")
    listed.git("checkout", "-q", "-b", "side")
    listed.commit("side work", {"s.txt": "s\n"})
    listed.git("checkout", "-q", "main")
    listed.commit("main work", {"m.txt": "m\n"})
    listed.git("merge", "-q", "--no-ff", "side", "-m", "Merge the Globex branch")
    result = listed.check("--commits", f"{base}..HEAD")
    assert result.returncode == 1
    assert "message line 1 matches entry 2" in result.stderr


# --- pre-push: the commits being pushed, from the runner's environment


def test_pre_push_uses_the_runner_refs(listed: Repo) -> None:
    base = listed.git("rev-parse", "HEAD")
    listed.commit("leak", {"x.txt": "globex\n"})
    head = listed.git("rev-parse", "HEAD")
    env = {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": head}
    result = listed.check("--pre-push", env=env)
    assert result.returncode == 1
    assert "x.txt:1: added line matches entry 2" in result.stderr
    env = {"PRE_COMMIT_FROM_REF": base, "PRE_COMMIT_TO_REF": base}
    assert listed.check("--pre-push", env=env).returncode == 0


def test_pre_push_without_refs_checks_what_no_remote_has(listed: Repo, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    listed.git("init", "-q", "--bare", str(remote))
    listed.git("remote", "add", "origin", str(remote))
    listed.git("push", "-q", "origin", "main")
    assert listed.check("--pre-push").returncode == 0
    listed.commit("leak", {"x.txt": "globex\n"})
    assert listed.check("--pre-push").returncode == 1


# --- A missing denylist: the hook warns and passes unless told otherwise


def test_missing_denylist_warns_and_passes(repo: Repo) -> None:
    repo.write("a.txt", "anything\n")
    repo.git("add", "a.txt")
    result = repo.check("--staged")
    assert result.returncode == 0
    assert "no denylist" in result.stderr


def test_require_denylist_fails_when_missing(repo: Repo) -> None:
    result = repo.check("--staged", "--require-denylist")
    assert result.returncode == 2
    assert "no denylist" in result.stderr


def test_denylist_file_flag_and_inline_env(repo: Repo, tmp_path: Path) -> None:
    f = tmp_path / "list.txt"
    f.write_text("# private terms\nglobex\n")
    repo.write("a.txt", "Globex\n")
    repo.git("add", "a.txt")
    assert repo.check("--staged", f"--denylist-file={f}").returncode == 1
    assert repo.check("--staged", env={"DATA_LEAK_DENYLIST": "acme, globex"}).returncode == 1
    assert repo.check("--staged", env={"DATA_LEAK_DENYLIST_FILE": str(f)}).returncode == 1
    assert repo.check("--staged", env={"DATA_LEAK_DENYLIST": "initech"}).returncode == 0


def test_an_invalid_denylist_is_an_error(repo: Repo) -> None:
    result = repo.check("--staged", env={"DATA_LEAK_DENYLIST": "re:("})
    assert result.returncode == 2
    assert "entry 1" in result.stderr
