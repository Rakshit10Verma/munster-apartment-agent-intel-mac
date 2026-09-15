#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

if [ -f data/agent.pid ]; then
  AGENT_PID="$(tr -cd '0-9' < data/agent.pid)"
  if [ -n "$AGENT_PID" ] && kill -0 "$AGENT_PID" 2>/dev/null; then
    echo "Daemon: running (PID $AGENT_PID)"
  else
    echo "Daemon: stopped (stale PID file)"
  fi
else
  echo "Daemon: stopped"
fi

python -m app.cli status
