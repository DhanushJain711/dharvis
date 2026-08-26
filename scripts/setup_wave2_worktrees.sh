#!/usr/bin/env bash
set -euo pipefail

# Run only after every Wave 1 branch has merged to a clean main checkout.
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

if [[ $(git branch --show-current) != "main" ]]; then
    echo "error: Wave 2 worktrees must be cut from main" >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet \
    || [[ -n $(git ls-files --others --exclude-standard) ]]; then
    echo "error: main must be clean before creating Wave 2 worktrees" >&2
    exit 1
fi

branches=(feat/scheduler-engine feat/proactive-jobs feat/facts-engine)
paths=(../bot-scheduler ../bot-jobs ../bot-facts)

for branch in "${branches[@]}"; do
    if git show-ref --verify --quiet "refs/heads/$branch"; then
        echo "error: branch already exists: $branch" >&2
        exit 1
    fi
done

for path in "${paths[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
        echo "error: worktree path already exists: $path" >&2
        exit 1
    fi
done

git worktree add ../bot-scheduler -b feat/scheduler-engine main
git worktree add ../bot-jobs      -b feat/proactive-jobs main
git worktree add ../bot-facts     -b feat/facts-engine main
