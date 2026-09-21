#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ "$#" -lt 1 ]; then
  echo "Usage: ./reprocess.sh <DB_ID> [--force] [--fallback-only]" >&2
  exit 64
fi

. .venv/bin/activate

# Re-runs the normal decision/generation pipeline on a listing's own stored text, so a
# row affected by a since-fixed rule/validator/fallback bug can be safely re-evaluated.
# This only re-drafts; it NEVER sends. sent/already_contacted/send_state_unknown are
# always refused. Use ./approve.sh afterwards to actually send a listing that now
# looks good.
#
# --fallback-only: diagnostic mode. Makes zero cloud-provider calls and exercises
# exactly the deterministic fallback path production uses on a real AI outage, so it
# can be tested against a stored listing without depending on providers actually
# being down. Independent of --force.
python -m app.cli reprocess "$@"
