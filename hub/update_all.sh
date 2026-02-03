#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/julianflowers/wildlife-cameras"
LIST="$REPO_DIR/hub/cameras.txt"
SERVICE="rpi-cam-server"
SSH_KEY="/home/julianflowers/.ssh/id_ed25519_camhub"

cd "$REPO_DIR"

echo "📥 Updating hub repo"
GIT_SSH_COMMAND="ssh -o BatchMode=yes" git pull

echo "🚀 Updating camera Pis"

while IFS= read -r LINE; do
  # trim whitespace
  LINE="${LINE#"${LINE%%[![:space:]]*}"}"
  LINE="${LINE%"${LINE##*[![:space:]]}"}"

  # skip blanks/comments
  [[ -z "$LINE" ]] && continue
  [[ "$LINE" =~ ^# ]] && continue

  # cameras.txt supports:
  # 1) ssh_only
  # 2) name, ssh, preview_url
  if [[ "$LINE" == *","* ]]; then
    NAME="$(echo "$LINE" | cut -d',' -f1 | xargs)"
    SSH_HOST="$(echo "$LINE" | cut -d',' -f2 | xargs)"
  else
    NAME="$LINE"
    SSH_HOST="$LINE"
  fi

  echo "➡️  $NAME ($SSH_HOST)"

  ssh -i "$SSH_KEY" -o BatchMode=yes "$SSH_HOST" \
    "cd ~/wildlife-cameras && \
     git pull && \
     sudo systemctl restart $SERVICE && \
     systemctl --no-pager --full status $SERVICE | head -n 12"

done < "$LIST"

echo "🎉 All cameras updated"
