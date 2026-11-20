# Copyright (c) 2026 Kilotronic LLC.
# SPDX-License-Identifier: MIT
"""Shared fixtures: throwaway git repositories with an isolated HOME, so no real config or denylist leaks in."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "src" / "data_leak_check" / "check.py"

ADA = {"name": "Ada Example", "email": "ada@example.org"}


def isolated_env(home: Path, **extra: str) -> dict[str, str]:
    """An environment with nothing from this machine's git config, HOME or denylist."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "DATA_LEAK_", "PRE_COMMIT", "GITHUB_"))}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        GIT_CONFIG_GLOBAL=str(home / ".gitconfig"),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME=ADA["name"],
        GIT_AUTHOR_EMAIL=ADA["email"],
        GIT_COMMITTER_NAME=ADA["name"],
        GIT_COMMITTER_EMAIL=ADA["email"],
    )
    env.update(extra)
    return env


class Repo:
    def __init__(self, path: Path, env: dict[str, str]) -> None:
        self.path = path
        self.env = env

    def git(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.path,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    def write(self, name: str, text: str) -> None:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str, files: dict[str, str] | None = None, **ident: str) -> str:
        for name, text in (files or {}).items():
            self.write(name, text)
        self.git("add", "-A")
        env = {}
        if "name" in ident:
            env.update(GIT_AUTHOR_NAME=ident["name"], GIT_COMMITTER_NAME=ident["name"])
        if "email" in ident:
            env.update(GIT_AUTHOR_EMAIL=ident["email"], GIT_COMMITTER_EMAIL=ident["email"])
        self.git("commit", "-q", "--allow-empty", "--no-verify", "-m", message, env=env)
        return self.git("rev-parse", "HEAD")

    def check(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """Run the checker as the hook or the Action would: a fresh process inside the repository."""
        return subprocess.run(
            [sys.executable, str(CHECK), *args],
            cwd=self.path,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Repo:
    path = tmp_path / "work"
    path.mkdir()
    r = Repo(path, isolated_env(home))
    r.git("init", "-q", "-b", "main")
    return r
