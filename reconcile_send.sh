#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ "$#" -ne 1 ]; then
  echo "Usage: ./reconcile_send.sh 'https://www.wg-gesucht.de/...'" >&2
  exit 64
fi

LISTING_URL="$1"
case "$LISTING_URL" in
  https://www.wg-gesucht.de/*|https://wg-gesucht.de/*) ;;
  *)
    echo "Refusing: reconcile_send.sh accepts exactly one HTTPS WG-Gesucht URL." >&2
    exit 64
    ;;
esac

. .venv/bin/activate

# Read-only recovery: opens the listing/conversation and compares it against the
# message we last attempted, but never clicks Send. Safe to run regardless of
# AUTO_SEND/DRY_RUN, and safe to re-run as many times as needed.
python -m app.cli reconcile --url "$LISTING_URL"
