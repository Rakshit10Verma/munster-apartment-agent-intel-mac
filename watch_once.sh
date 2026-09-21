#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

# Runs exactly one WG-Gesucht search-watcher discovery cycle and reports what it saw:
# which listing IDs are visible, which are baseline/already-seen, and which would be
# enqueued as genuinely new. Read-only for discovery purposes: it never analyzes,
# drafts, or sends anything, so it is safe regardless of AUTO_SEND/DRY_RUN.
python -m app.cli watch-once
