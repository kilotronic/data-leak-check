# Copyright (c) 2026 Kilotronic LLC.
# SPDX-License-Identifier: MIT
"""The published hooks, installed by prek and by pre-commit into a throwaway repository: all three stages.

Skipped when the runner is not installed (CI installs both). Slow: each runner builds the hook's environment.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, Repo, isolated_env

RUNNERS = ["prek", "pre-commit"]
COPY = ["pyproject.toml", "README.md", "LICENSE", ".pre-commit-hooks.yaml", "src"]


@pytest.fixture(scope="session")
def cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("runner-cache")


@pytest.fixture
def hook_repo(tmp_path: Path, home: Path) -> tuple[Path, str]:
    """This project, committed into its own repository, as prek and pre-commit fetch it."""
    path = tmp_path / "hook-repo"
    path.mkdir()
    for name in COPY:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, path / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, path / name)
    r = Repo(path, isolated_env(home))
    r.git("init", "-q", "-b", "main")
    return path, r.commit("hook")


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def runner_env(home: Path, cache: Path, runner: str) -> dict[str, str]:
    return isolated_env(
        home,
        PRE_COMMIT_HOME=str(cache / runner),
        PREK_HOME=str(cache / runner),
        DATA_LEAK_DENYLIST="globex, re:\\bwidget-\\d+\\b",
    )


@pytest.mark.parametrize("runner", RUNNERS)
def test_all_three_stages_from_a_config(runner: str, tmp_path: Path, home: Path, cache: Path, hook_repo) -> None:
    if not shutil.which(runner):
        pytest.skip(f"{runner} is not installed")
    hook_path, rev = hook_repo
    target = tmp_path / "target"
    target.mkdir()
    r = Repo(target, runner_env(home, cache, runner))
    r.git("init", "-q", "-b", "main")
    r.write(
        ".pre-commit-config.yaml",
        "default_install_hook_types: [pre-commit, commit-msg, pre-push]\n"
        "repos:\n"
        f"  - repo: {hook_path}\n"
        f"    rev: {rev}\n"
        "    hooks:\n"
        "      - id: data-leak-check\n"
        "      - id: data-leak-check-commit-msg\n"
        "      - id: data-leak-check-pre-push\n",
    )
    install = run([runner, "install"], target, r.env)
    assert install.returncode == 0, install.stdout + install.stderr
    for hook in ("pre-commit", "commit-msg", "pre-push"):
        assert (target / ".git" / "hooks" / hook).exists(), hook

    def commit(message: str) -> subprocess.CompletedProcess[str]:
        r.git("add", "-A")
        return run(["git", "commit", "-q", "-m", message], target, r.env)

    r.write("a.txt", "fine\n")
    ok = commit("A clean start")
    assert ok.returncode == 0, ok.stdout + ok.stderr

    # pre-commit stage: staged content.
    r.write("b.txt", "for Globex\n")
    staged = commit("Add notes")
    assert staged.returncode != 0
    assert "b.txt:1: added line matches entry 1" in staged.stdout + staged.stderr
    r.git("reset", "-q", "--hard")
    r.git("clean", "-qfd")

    # commit-msg stage: the message.
    r.write("c.txt", "fine\n")
    msg = commit("Fix widget-7")
    assert msg.returncode != 0
    assert "commit message line 1 matches entry 2" in msg.stdout + msg.stderr
    assert "widget" not in (msg.stdout + msg.stderr).lower()

    # pre-push stage: commits made with --no-verify still cannot be pushed.
    remote = tmp_path / "remote.git"
    r.git("init", "-q", "--bare", str(remote))
    r.git("remote", "add", "origin", str(remote))
    r.git("push", "-q", "origin", "main")
    r.git("add", "-A")
    r.git("commit", "-q", "--no-verify", "-m", "Notes from globex")
    push = run(["git", "push", "-q", "origin", "main"], target, r.env)
    assert push.returncode != 0
    assert "message line 1 matches entry 1" in push.stdout + push.stderr
    # A new branch: everything no remote has yet.
    push = run(["git", "push", "-q", "origin", "HEAD:refs/heads/new"], target, r.env)
    assert push.returncode != 0
    r.git("reset", "-q", "--hard", "HEAD~1")
    push = run(["git", "push", "-q", "origin", "HEAD:refs/heads/new"], target, r.env)
    assert push.returncode == 0, push.stdout + push.stderr


@pytest.mark.parametrize("runner", RUNNERS)
def test_try_repo(runner: str, tmp_path: Path, home: Path, cache: Path, hook_repo) -> None:
    if not shutil.which(runner):
        pytest.skip(f"{runner} is not installed")
    hook_path, rev = hook_repo
    target = tmp_path / "target"
    target.mkdir()
    r = Repo(target, runner_env(home, cache, runner))
    r.git("init", "-q", "-b", "main")
    r.commit("start", {"a.txt": "fine\n"})
    r.write("b.txt", "for globex\n")
    r.git("add", "b.txt")
    cmd = [runner, "try-repo", str(hook_path), "data-leak-check", "--ref", rev]
    leak = run(cmd, target, r.env)
    assert leak.returncode != 0, leak.stdout + leak.stderr
    assert "b.txt:1: added line matches entry 1" in leak.stdout + leak.stderr
    r.write("b.txt", "fine\n")
    r.git("add", "b.txt")
    clean = run(cmd, target, r.env)
    assert clean.returncode == 0, clean.stdout + clean.stderr
