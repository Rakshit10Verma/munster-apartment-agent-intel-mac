#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

# Runs the Telegram mobile control/notification bot as its own long-polling process,
# independent of ./start.sh's watcher daemon. Telegram is only a control surface: it
# reuses the exact same approve/reject/reconcile/contact functions as approve.sh,
# reject.sh, reconcile.sh, and AUTO_SEND -- there is no separate send path. Requires
# TELEGRAM_ENABLED=true and TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_USER_ID/
# TELEGRAM_CHAT_ID set in .env (see README). Run this in the background yourself if
# desired, e.g. `nohup ./telegram_bot.sh >> logs/telegram-console.log 2>&1 &`.
python -m app.telegram_bot
