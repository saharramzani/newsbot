#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu/Debian server. Run as root from inside the cloned repo:
#   bash deploy/install.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/newsbot

apt-get update
apt-get install -y python3 python3-venv ffmpeg git

id newsbot >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin newsbot
if [ "$SRC" != "$DEST" ]; then
  mkdir -p "$DEST"
  cp -a "$SRC"/. "$DEST"/
fi
cd "$DEST"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
[ -f .env ] || cp .env.example .env
chown -R newsbot:newsbot "$DEST"
chmod 600 .env

cp deploy/newsbot.service /etc/systemd/system/newsbot.service
systemctl daemon-reload
systemctl enable newsbot

echo
echo "Done. Now fill in the keys:   nano $DEST/.env"
echo "Then start the bot:           systemctl restart newsbot"
