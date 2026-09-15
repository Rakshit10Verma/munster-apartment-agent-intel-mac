#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11 or newer is required."
  exit 1
fi

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  echo "Using installed Google Chrome for Playwright (Ventura-compatible)."
else
  python -m playwright install chromium
fi

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi

mkdir -p data/inbox logs browser-profile/wg-gesucht
python -m ruff format --check app tests
python -m ruff check app tests
python -m mypy app
python -m pytest -q

echo "Setup complete. Add keys and BEWERBERMAPPE_PATH to .env, then run ./doctor.sh."
