# data-leak-check

A small, stdlib-only gate that keeps an organisation's private data out of its public repositories. You give it
a **denylist**: the terms that must never appear in public, such as your company's internal names, people's
names, private repository names and internal hostnames. It refuses any change, commit message or commit
author that matches one.

It ships as a **GitHub Action**, which checks the commits a pull request or push brings in, as **prek /
pre-commit hooks** for the pre-commit, commit-msg and pre-push stages, and as a **CLI**.

A public repository's log is public too, so **findings never show the term or the text that matched**. They
name the place (`notes.md:12`, `commit 1a2b3c4d5e6f: author email`) and the denylist entry by its position
(`entry 3`). A file path that itself matches is not printed either. Turn on `show-matches` only where the
output stays private.

## The denylist

```text
# Lines starting with # are comments; blank lines are ignored.
acme-corp, acme corporation
Wile E. Coyote
roadrunner-infra
re:\bacme\b
re:[a-z0-9]{20,}\.internal\.example\.net
```

- **Plain terms** match case-insensitively, anywhere in the text: `acme-corp` matches `ACME-Corp`.
- **`re:`** makes the rest of the line a Python regular expression, also case-insensitive. Because it runs to
  the end of the line, a regex may contain commas; put it last on its line or on a line of its own.
- Terms are separated by **newlines or commas**. Only lines that start with `#` are comments: a `#` after a
  term is part of the term, so keep comments on their own lines.
- Entries are **numbered from 1** in this order; findings name them by number.

Keep the list **out of the public repository**: a list of what must not be published is itself private.

## What it checks

| Where | Pre-commit hook | Commit-msg hook | Pre-push hook and Action |
| --- | --- | --- | --- |
| Added lines (with their line numbers) | staged changes | | each commit's own diff |
| New and renamed file paths | staged changes | | each commit's own diff |
| Commit message | | the message being written | every commit |
| Author and committer names and emails | the identity git will stamp | | every commit |

Each commit is checked on its own, so text added in one commit and removed in a later one is still refused:
both commits would publish it. Removed lines, unchanged lines and binary files are not checked, and nor is the
history already public.

## Use as a GitHub Action

```yaml
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  data-leak-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha> # pin actions by full commit SHA
        with:
          persist-credentials: false
      - uses: kilotronic/data-leak-check@<sha> # v1.0.0
        with:
          denylist: ${{ secrets.DATA_LEAK_DENYLIST }}
```

By default it checks the **commits the event brings in**: every commit in a pull request, or a push's new
commits. For a newly pushed branch, that is everything on it that the default branch does not have. Other
events check the checked-out commit. It fetches the history it needs itself, so the default shallow checkout
is fine.

**Where the list comes from.** Store it as an organisation or repository **secret** or **variable** named
`DATA_LEAK_DENYLIST` and pass it in:

```yaml
        with:
          denylist: ${{ secrets.DATA_LEAK_DENYLIST }}   # preferred for public repositories
```

```yaml
        with:
          denylist: ${{ vars.DATA_LEAK_DENYLIST }}      # private repositories only
```

**Prefer a secret for public repositories.** GitHub masks a secret's value in the log. It does not mask a
variable: it prints the Action's inputs at the top of the step's log, so a variable's denylist is readable by
anyone who can see the run, which on a public repository is everyone. For an organisation secret, give the
public repositories access to it in the organisation's settings.

**A missing denylist fails the check** by default (`if-empty: fail`), so a secret that was never set, or that
a repository was not given, cannot quietly pass. Two kinds of runs never get Actions secrets:

- **Dependabot** pull requests (they get Dependabot secrets instead; add `DATA_LEAK_DENYLIST` there too, or let
  them skip);
- **pull requests from forks**.

To let those pass unchecked rather than fail, set `if-empty: skip`. A typical split:

```yaml
      - uses: kilotronic/data-leak-check@<sha> # v1.0.0
        with:
          denylist: ${{ secrets.DATA_LEAK_DENYLIST }}
          # Fail if the secret is missing on our own branches; let forks and Dependabot through.
          if-empty: ${{ (github.event.pull_request.head.repo.fork || github.actor == 'dependabot[bot]') && 'skip' || 'fail' }}
```

Then run the hooks locally, or check the merged commits on `push` to main, where the secret is present.

Inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `denylist` | empty | The terms (see above), usually `${{ secrets.DATA_LEAK_DENYLIST }}` |
| `if-empty` | `fail` | `fail` or `skip` when the denylist is empty or missing |
| `show-matches` | `false` | `true` prints the matching entry and the matched text. Private repositories only |
| `commits` | empty (this event's commits) | A git range such as `main..HEAD` to check instead |

To make it a required check, add the job's name (`data-leak-check` above) to the branch's ruleset.

## Use as prek / pre-commit hooks

Works with [prek](https://github.com/j178/prek) and with [pre-commit](https://pre-commit.com). There is one
hook per stage:

```yaml
# .pre-commit-config.yaml
default_install_hook_types: [pre-commit, commit-msg, pre-push]
repos:
  - repo: https://github.com/kilotronic/data-leak-check
    rev: v1.0.0 # a dotted release tag, not the floating Action tag (v1)
    hooks:
      - id: data-leak-check            # pre-commit: staged changes and the commit's identity
      - id: data-leak-check-commit-msg # commit-msg: the message
      - id: data-leak-check-pre-push   # pre-push: the commits being pushed
```

Then `prek install` (or `pre-commit install`) installs all three stages, as `default_install_hook_types` lists
them. Without that line, name them: `prek install -t pre-commit -t commit-msg -t pre-push`.

**Where the list comes from**, the first that is set and not empty:

1. `--denylist-file PATH` in the hook's `args`;
2. the `DATA_LEAK_DENYLIST` environment variable, holding the terms themselves;
3. the `DATA_LEAK_DENYLIST_FILE` environment variable, holding a path;
4. `~/.config/data-leak-check/denylist` (or `$XDG_CONFIG_HOME/data-leak-check/denylist`).

All of these live outside the repository, so the list is never committed. Each person who works on the
repository keeps their own copy, for example in the default file.

**A missing denylist warns and passes**, so an outside contributor who has no list is not blocked (the Action
catches what they push). To fail instead, add `args: [--require-denylist]`.

The pre-push hook checks the commits being pushed, as the hook runner reports them. That includes commits made
with `--no-verify`. With no runner refs, it checks every commit no remote has yet.

## Use as a CLI

```bash
pip install git+https://github.com/kilotronic/data-leak-check.git@v1.0.0
data-leak-check                        # the staged changes (same as --staged)
data-leak-check --commits main..HEAD   # every commit in a range: authors, messages, content
data-leak-check --commit-msg .git/COMMIT_EDITMSG
data-leak-check --pre-push             # what no remote has yet (or PRE_COMMIT_FROM_REF..PRE_COMMIT_TO_REF)
data-leak-check --commits main..HEAD --show-matches   # private use: show the entries and matched text
```

Options: `--denylist-file PATH`, `--require-denylist`, `--show-matches`.

Exit codes:

- `0`: nothing on the denylist, or no denylist and none required.
- `1`: something matched.
- `2`: no denylist when one is required, an invalid entry (a bad regex, or one that matches empty text), or a
  range git can't read.

When something matches, remove it and commit again. A commit that is already made must be rewritten
(`git commit --amend`, or `git rebase -i`), because the old commit would still publish it.

## Development

```bash
pip install -e ".[dev]" prek pre-commit
pytest -q          # tests/test_prek.py installs the hooks with prek and pre-commit; it skips a runner that is missing
ruff check . && ruff format --check .
```

## License

MIT — see [LICENSE](LICENSE).
