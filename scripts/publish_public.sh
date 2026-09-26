#!/usr/bin/env bash
# Publish the current private HEAD to the public repo as one new commit.
# The private repo (full history, real data on disk) stays the working repo;
# the public repo gets snapshots only, so private history never leaves.
#   scripts/publish_public.sh "What changed"
set -euo pipefail
MSG="${1:?usage: publish_public.sh \"commit message\"}"
ROOT="$(git rev-parse --show-toplevel)"
PUBLIC_REPO="${PUBLIC_REPO:-https://github.com/meghajainbuilds/SMSFamilyAssistant.git}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone -q "$PUBLIC_REPO" "$WORK"
find "$WORK" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git -C "$ROOT" archive HEAD | tar -x -C "$WORK"
ln -s "$ROOT/private" "$WORK/private"
python3 "$WORK/scripts/leak_scan.py" --all
rm "$WORK/private"
ID="$(gh api user -q .id)"
git -C "$WORK" add -A
git -C "$WORK" -c user.name="Megha Jain" \
    -c user.email="${ID}+meghajainbuilds@users.noreply.github.com" \
    -c core.hooksPath=/dev/null commit -q -m "$MSG"
git -C "$WORK" push -q origin main
echo "published: $(git -C "$WORK" log --oneline -1)"
