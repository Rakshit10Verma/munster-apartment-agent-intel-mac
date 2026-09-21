#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

# Resets ONLY the search watcher's own baseline/already-seen bookkeeping -- never
# touches listings, drafts, or contact/processing history -- and immediately records
# the currently visible WG-Gesucht search results as the new baseline, so tomorrow's
# run only treats listings that appear AFTER this point as newly discovered.
#
# Opens the existing logged-in browser session to see the current search results, but
# is strictly read-only for listings: it never analyzes, drafts, or contacts anything.
# This is also what `./reset_run.sh --baseline-current` runs automatically after a
# full reset; use this on its own if you just want to re-baseline.
python -m app.cli baseline-watcher
