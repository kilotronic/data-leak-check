# Copyright (c) 2026 Kilotronic LLC.
# SPDX-License-Identifier: MIT
"""The Action's entry point (action.sh) on the event shapes GitHub sends, against a shallow checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, Repo, isolated_env

ZERO = "0" * 40
ACTION = ROOT / "src" / "data_leak_check" / "action.sh"


class Origin:
    """A bare 'GitHub' with main (base) and a feature branch holding a clean commit, then a leaking one."""

    def __init__(self, tmp_path: Path, home: Path) -> None:
        self.env = isolated_env(home)
        self.bare = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True, env=self.env)
        for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
            subprocess.run(["git", "-C", str(self.bare), "config", key, "true"], check=True, env=self.env)
        seed = tmp_path / "seed"
        seed.mkdir()
        r = Repo(seed, self.env)
        r.git("init", "-q", "-b", "main")
        r.git("remote", "add", "origin", self.bare.as_uri())
        self.base = r.commit("base", {"README.md": "hello\n"})
        r.git("push", "-q", "origin", "main")
        r.git("checkout", "-q", "-b", "feature")
        self.clean = r.commit("clean change", {"a.txt": "fine\n"})
        self.leak = r.commit("more", {"b.txt": "notes for globex\n"})
        r.git("push", "-q", "origin", "feature")
        self.seed = r
        self.tmp = tmp_path

    def add_commit(self, message: str, files: dict[str, str]) -> str:
        """Another commit on the feature branch, pushed."""
        sha = self.seed.commit(message, files)
        self.seed.git("push", "-q", "origin", "feature")
        return sha

    def checkout(self, sha: str) -> Path:
        """What actions/checkout leaves: a depth-1 clone of one commit."""
        self.runs = getattr(self, "runs", 0) + 1
        work = self.tmp / f"checkout-{self.runs}"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--no-single-branch", self.bare.as_uri(), str(work)],
            check=True,
            env=self.env,
        )
        subprocess.run(["git", "-C", str(work), "fetch", "-q", "--depth", "1", "origin", sha], check=True, env=self.env)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", sha], check=True, env=self.env)
        return work

    def run(self, at: str, **inputs: str) -> subprocess.CompletedProcess[str]:
        env = {
            **self.env,
            "GITHUB_ACTIONS": "true",
            "GITHUB_ACTION_PATH": str(ROOT),
            "DENYLIST": "globex",
            "IF_EMPTY": "fail",
            "SHOW_MATCHES": "false",
            "COMMITS": "",
            "EVENT": "",
            "PR_BASE": "",
            "PR_HEAD": "",
            "PUSH_BEFORE": "",
            "PUSH_AFTER": "",
            "REF": "",
            "DEFAULT_BRANCH": "main",
            **inputs,
        }
        return subprocess.run(
            ["bash", str(ACTION)], cwd=self.checkout(at), env=env, capture_output=True, text=True, check=False
        )


@pytest.fixture
def origin(tmp_path: Path, home: Path) -> Origin:
    return Origin(tmp_path, home)


def test_pull_request_with_a_leak_fails(origin: Origin) -> None:
    result = origin.run(origin.leak, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.leak)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "b.txt:1: added line matches entry 1" in result.stderr
    assert "globex" not in (result.stdout + result.stderr).lower()


def test_pull_request_checks_every_commit_not_only_its_head(origin: Origin) -> None:
    tidy = origin.add_commit("tidy", {"b.txt": "fine\n"})
    result = origin.run(tidy, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=tidy)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "b.txt:1: added line matches entry 1" in result.stderr


def test_other_events_check_the_checked_out_commit(origin: Origin) -> None:
    result = origin.run(origin.leak, EVENT="workflow_dispatch")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 commit(s)" in result.stderr
    assert origin.run(origin.clean, EVENT="schedule").returncode == 0


def test_pull_request_without_a_leak_passes(origin: Origin) -> None:
    result = origin.run(origin.clean, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.clean)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 commit(s)" in result.stdout


def test_push_checks_the_new_commits(origin: Origin) -> None:
    ref = {"EVENT": "push", "REF": "refs/heads/feature"}
    leak = origin.run(origin.leak, PUSH_BEFORE=origin.base, PUSH_AFTER=origin.leak, **ref)
    assert leak.returncode == 1, leak.stdout + leak.stderr
    only_last = origin.run(origin.leak, PUSH_BEFORE=origin.leak, PUSH_AFTER=origin.leak, **ref)
    assert only_last.returncode == 0, only_last.stdout + only_last.stderr
    clean = origin.run(origin.clean, PUSH_BEFORE=origin.base, PUSH_AFTER=origin.clean, **ref)
    assert clean.returncode == 0, clean.stdout + clean.stderr


def test_push_of_a_new_branch_checks_everything_not_on_the_default_branch(origin: Origin) -> None:
    result = origin.run(origin.leak, EVENT="push", REF="refs/heads/feature", PUSH_BEFORE=ZERO, PUSH_AFTER=origin.leak)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "2 commit(s)" in result.stderr


def test_push_that_deletes_a_branch_passes(origin: Origin) -> None:
    result = origin.run(origin.base, EVENT="push", REF="refs/heads/feature", PUSH_BEFORE=origin.leak, PUSH_AFTER=ZERO)
    assert result.returncode == 0, result.stdout + result.stderr


def test_commits_input_overrides_the_event(origin: Origin) -> None:
    result = origin.run(
        origin.leak,
        EVENT="pull_request",
        PR_BASE=origin.base,
        PR_HEAD=origin.leak,
        COMMITS=f"{origin.base}..{origin.clean}",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_author_and_message_in_a_pull_request(origin: Origin, tmp_path: Path) -> None:
    seed = Repo(tmp_path / "seed", origin.env)
    seed.git("checkout", "-q", "main")
    seed.git("checkout", "-q", "-b", "other")
    sha = seed.commit("Globex asked for this", email="someone@acme.example.com")
    seed.git("push", "-q", "origin", "other")
    result = origin.run(sha, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=sha, DENYLIST="globex, acme")
    assert result.returncode == 1
    assert "message line 1 matches entry 1" in result.stderr
    assert "author email matches entry 2" in result.stderr


@pytest.mark.parametrize("denylist", ["", "  \n", "# only a comment\n"])
def test_if_empty_fail_refuses_a_missing_denylist(origin: Origin, denylist: str) -> None:
    result = origin.run(
        origin.clean,
        EVENT="pull_request",
        PR_BASE=origin.base,
        PR_HEAD=origin.clean,
        DENYLIST=denylist,
        IF_EMPTY="fail",
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no denylist" in result.stderr
    assert "Dependabot" in result.stderr


def test_if_empty_skip_passes_a_missing_denylist(origin: Origin) -> None:
    result = origin.run(
        origin.leak, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.leak, DENYLIST="", IF_EMPTY="skip"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no denylist" in result.stdout + result.stderr


def test_if_empty_skip_still_checks_a_denylist_that_is_set(origin: Origin) -> None:
    result = origin.run(origin.leak, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.leak, IF_EMPTY="skip")
    assert result.returncode == 1


def test_if_empty_rejects_other_values(origin: Origin) -> None:
    result = origin.run(origin.clean, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.clean, IF_EMPTY="maybe")
    assert result.returncode == 2
    assert "if-empty" in result.stderr


def test_show_matches_input(origin: Origin) -> None:
    result = origin.run(
        origin.leak, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.leak, SHOW_MATCHES="true"
    )
    assert result.returncode == 1
    assert "'globex'" in result.stderr


def test_the_action_ignores_a_denylist_file_on_the_runner(origin: Origin) -> None:
    home = Path(origin.env["HOME"])
    f = home / ".config" / "data-leak-check" / "denylist"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("globex\n")
    result = origin.run(
        origin.leak, EVENT="pull_request", PR_BASE=origin.base, PR_HEAD=origin.leak, DENYLIST="", IF_EMPTY="fail"
    )
    assert result.returncode == 2
