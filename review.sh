#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

# With no argument: lists every listing needing human attention, grouped by status.
# With a DB id: shows the full detail for that one listing. Read-only either way --
# never analyzes, drafts, or sends anything.
if [ "$#" -eq 0 ]; then
  python -m app.cli review
else
  python -m app.cli review "$1"
fi
