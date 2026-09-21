#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ "$#" -ne 1 ]; then
  echo "Usage: ./send_once.sh 'https://www.wg-gesucht.de/...'" >&2
  exit 64
fi

LISTING_URL="$1"
case "$LISTING_URL" in
  https://www.wg-gesucht.de/*|https://wg-gesucht.de/*) ;;
  *)
    echo "Refusing: send_once.sh accepts exactly one HTTPS WG-Gesucht URL." >&2
    exit 64
    ;;
esac

. .venv/bin/activate

AUTO_SEND_ENABLED="$(python -c 'from app.config_loader import load_settings; print("true" if load_settings().auto_send else "false")')"
if [ "$AUTO_SEND_ENABLED" != "true" ]; then
  echo "Refusing real send: set AUTO_SEND=true in .env first." >&2
  exit 2
fi

PID_FILE="data/agent.pid"
if [ -f "$PID_FILE" ]; then
  AGENT_PID="$(tr -cd '0-9' < "$PID_FILE")"
  if [ -n "$AGENT_PID" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
    AGENT_COMMAND="$(ps -p "$AGENT_PID" -o command= 2>/dev/null || true)"
    if [[ "$AGENT_COMMAND" == *"app.cli run"* ]]; then
      echo "Refusing one-shot send while the daemon is running; run ./stop.sh first." >&2
      exit 2
    fi
  fi
fi

# AUTO_SEND=true is the explicit authorization. Production mode is scoped to
# this one foreground process; the daemon configuration and behavior are untouched.
DRY_RUN=false AUTO_SEND=true python -m app.cli analyze \
  --url "$LISTING_URL" --platform wg_gesucht --prepare --require-sent
