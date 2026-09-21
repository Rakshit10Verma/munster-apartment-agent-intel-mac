#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

# With no argument: lists every send_state_unknown listing. With a DB id: reconciles
# that one by reading the live WG-Gesucht conversation. This NEVER clicks Send; see
# ./reconcile_send.sh for the original URL-based form (still used by that script).
if [ "$#" -eq 0 ]; then
  python -m app.cli reconcile --list
else
  python -m app.cli reconcile --id "$1"
fi
