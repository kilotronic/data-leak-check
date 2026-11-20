#!/usr/bin/env python3
# Copyright (c) 2026 Kilotronic LLC.
# SPDX-License-Identifier: MIT
"""Refuse a denylist of private terms in what is about to become public.

A public repository's history is public forever, and so is its CI log. This checks what is being added
against a denylist of terms that must never appear there (company, people and project names, internal
hostnames, ...) and refuses on a match.

What it checks, by mode:

``--staged`` (the default; the pre-commit stage)
    The lines added in the staged changes, the paths of new and renamed files, and the author and
    committer git will stamp.
``--commit-msg FILE`` (the commit-msg stage)
    The commit message, without git's ``#`` comment lines and anything below the scissors line.
``--pre-push`` (the pre-push stage)
    The commits being pushed (``PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF`` from the hook runner, or every
    commit no remote has yet).
``--commits RANGE`` (what the GitHub Action passes)
    Every commit in a git range: author and committer names and emails, the message, and the lines and
    paths each commit adds (so content added and removed again inside the range still counts).

The denylist is a list of terms, one per line or separated by commas. Plain terms match case-insensitively
anywhere; ``re:`` makes the rest of the line a regular expression (also case-insensitive). Lines starting
with ``#`` and blank lines are ignored. ``allow:`` makes the rest of the line an author or committer name or
email that passes although an entry matches it, when it is the whole value (ignoring case). It allows nothing
else: the same text in a file, a message or a longer address is still refused. The list is read from the
first of: ``--denylist-file``, the ``DATA_LEAK_DENYLIST`` environment variable (the terms themselves), the
``DATA_LEAK_DENYLIST_FILE`` environment variable (a path), and ``~/.config/data-leak-check/denylist``. Keep
it out of the repository.

Findings never show the term or the matched text, because the log may itself be public: they name the
place and the denylist entry by its position ("entry 3"). ``--show-matches`` adds both, for private use.
A path that matches is not printed either.

No denylist: warn and pass, so contributors without the list are not blocked. ``--require-denylist`` fails
instead (the GitHub Action passes it unless ``if-empty: skip``).

Exit codes: 0 nothing found (or no denylist), 1 something found, 2 a usage or configuration error.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ENV_TERMS = "DATA_LEAK_DENYLIST"
ENV_FILE = "DATA_LEAK_DENYLIST_FILE"
SCISSORS = re.compile(r"^# -+ >8 -+$")
SHA_LEN = 12


class DenylistError(ValueError):
    """The denylist cannot be used. The message never holds the entry's text."""


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    number: int  # 1-based position in the list: what findings print
    source: str  # the entry as written; printed only with --show-matches
    pattern: re.Pattern[str]
    allow: bool = False  # an ``allow:`` entry: an exact identity to let through, never a match itself


# --- The denylist


def _split_terms(text: str) -> list[str]:
    terms: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        while line:
            # A regex or an allowed value runs to the end of the line: it may hold commas.
            if line.startswith(("re:", "allow:")):
                terms.append(line)
                break
            head, _, line = line.partition(",")
            if head.strip():
                terms.append(head.strip())
            line = line.strip()
    return terms


def parse_denylist(text: str) -> list[Entry]:
    entries = []
    for number, term in enumerate(_split_terms(text), 1):
        if term.startswith("allow:"):
            value = term[6:].strip()
            if not value:
                raise DenylistError(f"entry {number} allows nothing")
            entries.append(Entry(number, term, re.compile(re.escape(value), re.IGNORECASE), allow=True))
            continue
        if term.startswith("re:"):
            expr = term[3:].strip()
            try:
                pattern = re.compile(expr, re.IGNORECASE)
            except re.error as err:
                raise DenylistError(f"entry {number} is not a valid regular expression ({err.msg})") from None
            if pattern.search(""):
                raise DenylistError(f"entry {number} matches empty text, so it would match everything")
        else:
            pattern = re.compile(re.escape(term), re.IGNORECASE)
        entries.append(Entry(number, term, pattern))
    return entries


def default_file(environ: Mapping[str, str]) -> Path:
    config = environ.get("XDG_CONFIG_HOME") or str(Path(environ.get("HOME") or Path.home()) / ".config")
    return Path(config) / "data-leak-check" / "denylist"


def _candidates(file_arg: str | None, environ: Mapping[str, str]) -> Iterator[tuple[str, str | None, Path | None]]:
    """(label, inline text, file) for each place the list may come from, in order."""
    if file_arg:
        path = Path(file_arg).expanduser()
        yield f"--denylist-file {path}", None, path
        return  # named explicitly: nothing else is consulted
    yield f"${ENV_TERMS}", environ.get(ENV_TERMS, ""), None
    if environ.get(ENV_FILE):
        path = Path(environ[ENV_FILE]).expanduser()
        yield f"${ENV_FILE} ({path})", None, path
    yield str(default_file(environ)), None, default_file(environ)


def load_denylist(
    file_arg: str | None, environ: Mapping[str, str], *, env_only: bool = False
) -> tuple[list[Entry], str | None]:
    """The first denylist that is set and not empty, and where it came from; ([], None) when there is none."""
    for label, text, path in _candidates(file_arg, environ):
        if env_only and label != f"${ENV_TERMS}":
            continue
        if path is not None:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        entries = parse_denylist(text or "")
        if any(not e.allow for e in entries):  # allow: entries alone check nothing
            return entries, label
    return [], None


def looked_at(file_arg: str | None, environ: Mapping[str, str], *, env_only: bool = False) -> str:
    labels = [label for label, _, _ in _candidates(file_arg, environ)]
    return ", ".join(labels[:1] if env_only else labels)


def matches(text: str, entries: Sequence[Entry]) -> list[tuple[Entry, str]]:
    """The entries that match ``text``, each with the text it matched."""
    found = []
    for entry in entries:
        if entry.allow:
            continue
        m = entry.pattern.search(text)
        if m:
            found.append((entry, m.group(0)))
    return found


# --- Findings


@dataclass
class Report:
    entries: Sequence[Entry]
    show: bool = False
    findings: list[str] = field(default_factory=list)

    def path(self, path: str) -> str:
        """``path`` for printing; a path that itself matches is not printed."""
        hits = matches(path, self.entries)
        if not hits or self.show:
            return path
        return f"[a path that matches {self._which(hits)}]"

    def allowed(self, value: str) -> bool:
        """``value`` is exactly an ``allow:`` entry (ignoring case and surrounding spaces)."""
        value = value.strip().casefold()
        return any(e.allow and e.source[6:].strip().casefold() == value for e in self.entries)

    def check_identity(self, where: str, what: str, value: str) -> None:
        """An author or committer name or email: the one place ``allow:`` entries apply."""
        if not self.allowed(value):
            self.check(where, what, value)

    def check(self, where: str, what: str, text: str) -> None:
        hits = matches(text, self.entries)
        if hits:
            self.findings.append(f"{where}{what} matches {self._which(hits)}")

    def _which(self, hits: list[tuple[Entry, str]]) -> str:
        numbers = ", ".join(str(e.number) for e, _ in hits)
        label = f"entr{'ies' if len(hits) > 1 else 'y'} {numbers}"
        if self.show:
            label += " (" + "; ".join(f"{e.source!r} matched {m!r}" for e, m in hits) + ")"
        return label


# --- git


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


DIFF_OPTS = (
    "-U0",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--find-renames",
)


def _unquote(path: str) -> str:
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1]
        path = re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n"}.get(m.group(1), m.group(1)), path)
    return path


@dataclass(frozen=True)
class Added:
    path: str
    line: int | None  # None: the path itself is new
    text: str


def added(diff: str) -> Iterator[Added]:
    """The new paths and the added lines (with their line numbers in the new file) in a ``git diff -U0``."""
    path: str | None = None
    lineno = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path, in_hunk = None, False
        elif not in_hunk:
            if line.startswith("+++ "):
                target = line[4:]
                path = None if target == "/dev/null" else _unquote(target).removeprefix("b/")
            elif line.startswith(("rename to ", "copy to ")):
                yield Added(_unquote(line.split(" to ", 1)[1]), None, "")
            elif line.startswith("@@"):
                in_hunk = True
        if in_hunk:
            if line.startswith("@@"):
                m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
                lineno = int(m.group(1)) if m else 0
            elif line.startswith("+") and path is not None:
                yield Added(path, lineno, line[1:])
                lineno += 1
            elif line.startswith(" "):
                lineno += 1


def _new_paths(diff: str) -> Iterator[str]:
    """Paths of files the diff creates (renames and copies come from ``added``)."""
    new = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            new = False
        elif line.startswith("new file mode"):
            new = True
        elif new and line.startswith("+++ ") and line[4:] != "/dev/null":
            yield _unquote(line[4:]).removeprefix("b/")
            new = False


def check_diff(report: Report, diff: str, prefix: str = "") -> None:
    for path in _new_paths(diff):
        report.check(f"{prefix}{report.path(path)}: ", "file path", path)
    for item in added(diff):
        shown = report.path(item.path)
        if item.line is None:
            report.check(f"{prefix}{shown}: ", "file path", item.path)
        else:
            report.check(f"{prefix}{shown}:{item.line}: ", "added line", item.text)


def check_message(report: Report, message: str, prefix: str) -> None:
    for n, line in enumerate(message.splitlines(), 1):
        report.check(prefix, f"message line {n}", line)


def _identity(kind: str) -> tuple[str, str]:
    ident = _git("var", f"GIT_{kind}_IDENT").strip()
    m = re.match(r"^(.*?) <([^>]*)>", ident)
    return (m.group(1), m.group(2)) if m else ("", "")


def check_staged(report: Report) -> str:
    check_diff(report, _git("diff", "--cached", *DIFF_OPTS))
    for kind in ("AUTHOR", "COMMITTER"):
        try:
            name, email = _identity(kind)
        except GitError:
            continue  # no identity configured: git itself will refuse the commit
        report.check_identity("this commit's ", f"{kind.lower()} name", name)
        report.check_identity("this commit's ", f"{kind.lower()} email", email)
    return "the staged changes"


def check_commit_msg(report: Report, path: str) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for n, line in enumerate(text.splitlines(), 1):
        if SCISSORS.match(line):
            break
        if not line.startswith("#"):
            report.check("", f"commit message line {n}", line)
    return "the commit message"


def check_commits(report: Report, revs: Sequence[str]) -> str:
    log = _git("log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e", *revs, "--")
    shas = []
    for record in log.split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue
        sha, an, ae, cn, ce, body = record.split("\x00", 5)
        shas.append(sha)
        where = f"commit {sha[:SHA_LEN]}: "
        report.check_identity(where, "author name", an)
        report.check_identity(where, "author email", ae)
        report.check_identity(where, "committer name", cn)
        report.check_identity(where, "committer email", ce)
        check_message(report, body, where)
    # Each commit's own diff (merges show none), so content added and removed again in the range counts.
    patches = _git("log", "--no-merges", "-p", "--format=%x00%H", *DIFF_OPTS, *revs, "--")
    for chunk in patches.split("\x00")[1:]:
        sha, _, diff = chunk.partition("\n")
        check_diff(report, diff, f"commit {sha[:SHA_LEN]}: ")
    return f"{len(shas)} commit(s) in {' '.join(revs)}"


def pre_push_revs(environ: Mapping[str, str]) -> list[str]:
    """The commits being pushed, from the hook runner's refs; without them, what no remote has yet."""
    to_ref = environ.get("PRE_COMMIT_TO_REF") or "HEAD"
    from_ref = environ.get("PRE_COMMIT_FROM_REF")
    if from_ref:
        return [f"{from_ref}..{to_ref}"]
    return [to_ref, "--not", "--remotes"]


# --- Command line


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="data-leak-check",
        description="Refuse a denylist of private terms in staged changes, commit messages or commits.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check the staged changes and identity (default)")
    mode.add_argument("--commit-msg", action="store_true", help="check the commit message file given as FILE")
    mode.add_argument("--pre-push", action="store_true", help="check the commits being pushed")
    mode.add_argument("--commits", metavar="RANGE", help="check every commit in a git range, e.g. main..HEAD")
    parser.add_argument("files", nargs="*", metavar="FILE", help="the commit message file (with --commit-msg)")
    parser.add_argument("--denylist-file", metavar="PATH", help="read the denylist from this file")
    parser.add_argument(
        "--require-denylist", action="store_true", help="fail, instead of warning, when there is no denylist"
    )
    parser.add_argument(
        "--show-matches",
        action="store_true",
        help="print the matching denylist entry and the matched text (private use only: logs may be public)",
    )
    parser.add_argument("--denylist-from-env", action="store_true", help=argparse.SUPPRESS)  # the Action
    args = parser.parse_args(argv)
    if args.commit_msg and len(args.files) != 1:
        parser.error("--commit-msg needs exactly one FILE")
    return args


def _ci() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _say(level: str, message: str) -> None:
    prefix = f"::{level}::" if _ci() else ""
    print(f"{prefix}data-leak-check: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    args = _parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        entries, _source = load_denylist(args.denylist_file, env, env_only=args.denylist_from_env)
    except DenylistError as err:
        _say("error", f"the denylist cannot be used: {err}")
        return 2
    if not entries:
        where = looked_at(args.denylist_file, env, env_only=args.denylist_from_env)
        if _ci():
            where = "the `denylist` input"
        hint = (
            " GitHub does not give Actions secrets to Dependabot or to pull requests from forks;"
            " set `if-empty: skip` to let those pass unchecked."
            if _ci()
            else ""
        )
        if args.require_denylist:
            _say("error", f"no denylist (looked at {where}), and one is required.{hint}")
            return 2
        _say("warning", f"no denylist (looked at {where}); nothing checked.{hint}")
        return 0

    report = Report(entries, show=args.show_matches)
    try:
        if args.commit_msg:
            what = check_commit_msg(report, args.files[0])
        elif args.pre_push:
            what = check_commits(report, pre_push_revs(env))
        elif args.commits:
            what = check_commits(report, args.commits.split())
        else:
            what = check_staged(report)
    except (GitError, OSError) as err:
        _say("error", str(err))
        return 2

    if not report.findings:
        print(f"data-leak-check: {what}: nothing on the denylist ({sum(not e.allow for e in entries)} entries)")
        return 0
    _say("error", f"{len(report.findings)} finding(s) in {what}")
    for item in report.findings:
        print(f"  - {item}", file=sys.stderr)
    if not args.show_matches:
        print(
            "Terms and matched text are not shown, because this output may be public. Entries are numbered from 1\n"
            "in denylist order; to see them, run `data-leak-check --show-matches` where the output stays private.",
            file=sys.stderr,
        )
    print(
        "Remove the matched text, then commit again. A commit that is already made must be rewritten\n"
        "(git commit --amend, or git rebase -i): the old commit would still publish it.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
