#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

TARGET="${1:-wg}"
if [ "$TARGET" != "wg" ] && [ "$TARGET" != "gmail" ]; then
  echo "Usage: ./login.sh [wg|gmail]"
  exit 2
fi

. .venv/bin/activate
python -m app.cli login "$TARGET"
