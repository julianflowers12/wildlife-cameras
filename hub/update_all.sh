#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/wildlife-cameras"
SERVICE="rpi-cam-server"
LIST="$REPO_DIR/hub/cameras.txt"

cd "$REPO_DIR"
git pull

while read -r HOST; do
  [[ -z "${HOST:-}" ]] && continue
  echo "➡️  Updating $HOST"
  ssh "$HOST" "cd ~/wildlife-cameras && git pull && sudo systemctl restart $SERVICE && systemctl --no-pager --full status $SERVICE | head -n 12"
done < "$LIST"
