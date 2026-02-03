#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/wildlife-cameras"
SERVICE="rpi-cam-server"
LIST="$REPO_DIR/hub/cameras.txt"

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

  # cameras.txt formats supported:
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

  ssh -i "$HOME/.ssh/id_ed25519_camhub" -o BatchMode=yes "$SSH_HOST" \
    "cd ~/wildlife-cameras && git pull && sudo systemctl restart rpi-cam-server  && systemctl --no-pager --full status $SERVICE | head -n 12"

done < "$LIST"

echo "🎉 All cameras updated"
