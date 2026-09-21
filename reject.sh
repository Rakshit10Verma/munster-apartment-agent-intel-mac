#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ "$#" -ne 1 ]; then
  echo "Usage: ./reject.sh <DB_ID>" >&2
  exit 64
fi

. .venv/bin/activate

# Asks for confirmation, then marks the listing manually_rejected. It will never be
# reprocessed even if it reappears in search results (the permanent dedup record
# already prevents that regardless of status).
python -m app.cli reject "$1"
