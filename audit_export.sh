#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ $# -gt 1 ]]; then
  echo "Usage: ./audit_export.sh [DB_ID]" >&2
  exit 2
fi

if [[ $# -eq 1 && ! "$1" =~ ^[1-9][0-9]*$ ]]; then
  echo "DB_ID must be a positive integer." >&2
  exit 2
fi

exec .venv/bin/python -m app.audit_export "$@"
