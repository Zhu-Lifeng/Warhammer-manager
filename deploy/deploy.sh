#!/usr/bin/env bash
# Deploy hook fired by deploy/webhook.py on a verified GitHub push.
#
# Critical invariant: this script must NEVER touch WH40K_DATA_DIR.
# User-uploaded data (app.db + uploads/) lives outside the repo on purpose
# so a bad commit can't wipe armies, models, or images.
#
# Required environment (provided by the systemd unit):
#   APP_DIR              absolute path to the repo checkout
#   VENV_DIR             absolute path to the virtualenv
#   APP_SERVICE          systemd unit name for the Flask app (e.g. warhammer-manager)
set -euo pipefail

: "${APP_DIR:?APP_DIR is required}"
: "${VENV_DIR:?VENV_DIR is required}"
: "${APP_SERVICE:?APP_SERVICE is required}"

cd "$APP_DIR"

echo "[deploy] pulling latest commits"
# --ff-only refuses to deploy if the local checkout has diverged — avoids
# silent merge commits made by the service account.
git fetch --prune origin
git reset --hard "origin/$(git rev-parse --abbrev-ref HEAD)"

echo "[deploy] syncing python dependencies"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt

echo "[deploy] restarting $APP_SERVICE"
sudo /bin/systemctl restart "$APP_SERVICE"

echo "[deploy] done at $(date -Iseconds)"
