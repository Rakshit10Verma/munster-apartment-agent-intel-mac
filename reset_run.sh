#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

# Clears old apartment-processing/history clutter (discovered, filtered_skip,
# review_required, drafted, ai_failed, premium_boost_failed, dry_run_ready, a
# reconciled not_sent, and their draft messages/extraction records/events) so
# status.sh, review.sh, and the dashboard start clean for a new run.
#
# Never clears: anything actually sent, already_contacted, send_state_unknown, a
# manually_rejected decision, or any listing whose contact_attempts record shows Send
# was actually clicked (including a process-crash-before-verification case) --
# regardless of that listing's own status label. A listing where the real
# browser/contact flow started but Send was never clicked (e.g. Premium failed before
# the message send) is NOT treated as a duplicate-send risk and is cleared.
#
# Creates a timestamped backup under backups/ before touching anything, shows a
# preview, and requires explicit confirmation (default: No). Transactional: if
# anything fails, the reset is rolled back and nothing is deleted.
#
# By default this only ever operates on the local SQLite database: no WG-Gesucht
# browsing, no sending, no browser/session/config/document changes. Pass
# --baseline-current to ALSO record the currently visible WG-Gesucht search results as
# the watcher's new baseline immediately afterward (read-only: never analyzes, drafts,
# or contacts anything) -- this is the one case that opens the browser, and only
# because you asked for it. Without that flag, run ./baseline_watcher.sh separately
# whenever you want to do just that step.
python -m app.cli reset-run "$@"
