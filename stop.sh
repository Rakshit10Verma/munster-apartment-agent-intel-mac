#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
PID_FILE="data/agent.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "Agent is not running (no PID file)."
  exit 0
fi

AGENT_PID="$(tr -cd '0-9' < "$PID_FILE")"
if [ -z "$AGENT_PID" ] || ! kill -0 "$AGENT_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Removed stale PID file; agent was not running."
  exit 0
fi

COMMAND="$(ps -p "$AGENT_PID" -o command= 2>/dev/null || true)"
if [[ "$COMMAND" != *"app.cli run"* ]]; then
  echo "Refusing to stop PID $AGENT_PID because it is not the apartment agent."
  exit 1
fi

kill "$AGENT_PID"
for _ in 1 2 3 4 5; do
  if ! kill -0 "$AGENT_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Agent stopped."
    exit 0
  fi
  sleep 1
done

echo "Agent is still shutting down; check ./status.sh shortly."
