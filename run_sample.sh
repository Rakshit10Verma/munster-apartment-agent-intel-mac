#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate
DRY_RUN=true AUTO_SEND=false python -m app.cli analyze \
  --file samples/sample_listing.txt --platform wg_gesucht
