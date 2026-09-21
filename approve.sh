#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ "$#" -ne 1 ]; then
  echo "Usage: ./approve.sh <DB_ID>" >&2
  exit 64
fi

. .venv/bin/activate

# Shows the exact message that would be sent and asks for explicit confirmation
# (default: No). Only routes into the existing contact/send pipeline after a 'y'.
python -m app.cli approve "$1"
