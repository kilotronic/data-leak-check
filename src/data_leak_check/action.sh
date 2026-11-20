#!/usr/bin/env bash
# GitHub Action entry point: work out which commits this event brings in, make sure their history and content
# are present (actions/checkout is shallow by default), then run check.py on them. Inputs arrive as env vars
# (action.yml). The denylist reaches check.py only through the environment, never the command line.
set -euo pipefail

case "$IF_EMPTY" in
  fail) require=(--require-denylist) ;;
  skip) require=() ;;
  *)
    echo "::error::data-leak-check: if-empty must be 'fail' or 'skip'"
    echo "data-leak-check: if-empty must be 'fail' or 'skip'" >&2
    exit 2
    ;;
esac
show=()
case "$SHOW_MATCHES" in true | True | TRUE | yes | 1) show=(--show-matches) ;; esac

check=(python3 "$GITHUB_ACTION_PATH/src/data_leak_check/check.py" --denylist-from-env)
export DATA_LEAK_DENYLIST="$DENYLIST"
unset DATA_LEAK_DENYLIST_FILE

zero=0000000000000000000000000000000000000000
range="$COMMITS"
base_branch=""
if [ -z "$range" ]; then
  case "$EVENT" in
    pull_request | pull_request_target) range="$PR_BASE..$PR_HEAD" ;;
    push)
      if [ -z "$PUSH_AFTER" ] || [ "$PUSH_AFTER" = "$zero" ]; then
        echo "data-leak-check: a branch or tag was deleted; nothing to check"
        exit 0
      elif [ -n "$PUSH_BEFORE" ] && [ "$PUSH_BEFORE" != "$zero" ]; then
        range="$PUSH_BEFORE..$PUSH_AFTER"
      elif [ -n "$DEFAULT_BRANCH" ] && [ "$REF" != "refs/heads/$DEFAULT_BRANCH" ]; then
        # A new branch: everything on it that the default branch does not have.
        base_branch="$DEFAULT_BRANCH"
        range="refs/remotes/origin/$DEFAULT_BRANCH..$PUSH_AFTER"
      else
        range="$PUSH_AFTER" # the default branch's first push: its whole history
      fi
      ;;
    *)
      echo "data-leak-check: event '$EVENT' brings in no commits of its own; checking the checked-out commit only"
      range="HEAD^!"
      ;;
  esac
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "::error::data-leak-check needs a checkout (run actions/checkout first) to check commits $range"
  exit 2
fi

# Fetch the history the range needs, without blobs (git fetches the ones the diffs read on demand), plus any
# SHAs the range names.
shas=() # (no mapfile: macOS runners ship bash 3.2)
while read -r sha; do shas+=("$sha"); done < <(grep -oE '[0-9a-f]{40}' <<<"$range" || true)
refspecs=(${shas[@]+"${shas[@]}"})
if [ -n "$base_branch" ]; then
  refspecs+=("+refs/heads/$base_branch:refs/remotes/origin/$base_branch")
fi
fetch=(git fetch -q --no-tags --filter=blob:none origin ${refspecs[@]+"${refspecs[@]}"})
if [ "$(git rev-parse --is-shallow-repository)" = true ]; then
  fetch+=(--unshallow)
fi
if [ "${#refspecs[@]}" -gt 0 ] || [ "$(git rev-parse --is-shallow-repository)" = true ]; then
  if ! "${fetch[@]}" && [ -n "$base_branch" ]; then
    # No default branch to compare with yet: check the whole new branch.
    range="$PUSH_AFTER"
    git fetch -q --no-tags --filter=blob:none origin ${shas[@]+"${shas[@]}"}
  fi
fi

exec "${check[@]}" --commits "$range" ${require[@]+"${require[@]}"} ${show[@]+"${show[@]}"}
