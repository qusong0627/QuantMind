#!/usr/bin/env bash
set -euo pipefail
# Sync repository to Gitee remote. Safe defaults; can enable force push.
# Usage:
#   DRY_RUN=1 ./scripts/sync_to_gitee.sh        # show what would be pushed
#   ./scripts/sync_to_gitee.sh                  # push all branches and tags
#   FORCE_PUSH=1 ./scripts/sync_to_gitee.sh     # force-push all branches and tags
#   REMOTE=origin ./scripts/sync_to_gitee.sh    # use a different remote

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

REMOTE=${REMOTE:-gitee}
DRY_RUN=${DRY_RUN:-0}
FORCE_PUSH=${FORCE_PUSH:-0}

echo "Repo: $REPO_DIR"
echo "Remote: $REMOTE"

echo "Fetching remote: $REMOTE"
git fetch "$REMOTE" --prune

if [ "$DRY_RUN" != "0" ]; then
  echo "--- DRY RUN: commits that would be pushed to $REMOTE ---"
  git for-each-ref --format='%(refname:short)' refs/heads/ | while read -r b; do
    echo "Branch: $b"
    git log --oneline "${REMOTE}/${b}".."${b}" || true
    echo
  done
  echo "Tags to push:"
  git tag -l
  exit 0
fi

if [ "$FORCE_PUSH" = "1" ]; then
  echo "Force-pushing all branches and tags to $REMOTE..."
  git push "$REMOTE" --all --force
  git push "$REMOTE" --tags --force
else
  echo "Pushing all branches and tags to $REMOTE..."
  git push "$REMOTE" --all
  git push "$REMOTE" --tags
fi

echo "Sync to $REMOTE completed."
