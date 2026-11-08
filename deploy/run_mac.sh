#!/usr/bin/env bash
# Runs the bot and restarts it automatically if it crashes. Ctrl+C twice to stop.
cd "$(dirname "$0")/.."
while true; do
  caffeinate -i .venv/bin/python bot.py
  echo "Bot stopped. Restarting in 10 seconds..."; sleep 10
done
