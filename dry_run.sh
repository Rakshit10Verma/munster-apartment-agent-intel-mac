#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
. .venv/bin/activate

export DRY_RUN=true
export AUTO_SEND=false

if [ "$#" -eq 0 ]; then
  python -m app.cli analyze --file samples/sample_listing.txt --platform wg_gesucht
elif [[ "$1" == http://* || "$1" == https://* ]]; then
  python -m app.cli analyze --url "$1" --platform wg_gesucht --prepare
else
  python -m app.cli analyze --file "$1" --platform wg_gesucht
fi
