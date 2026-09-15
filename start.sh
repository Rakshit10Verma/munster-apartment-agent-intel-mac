#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
mkdir -p data logs

PID_FILE="data/agent.pid"
if [ -f "$PID_FILE" ]; then
  EXISTING_PID="$(tr -cd '0-9' < "$PID_FILE")"
  if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "Agent is already running (PID $EXISTING_PID)."
    exit 0
  fi
fi

. .venv/bin/activate
nohup python -m app.cli run >> logs/daemon-console.log 2>&1 &
AGENT_PID=$!
echo "$AGENT_PID" > "$PID_FILE"
echo "Agent started (PID $AGENT_PID). Use ./status.sh and ./stop.sh."
