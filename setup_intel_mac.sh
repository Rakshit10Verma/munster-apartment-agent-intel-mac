#!/bin/bash
set -e
echo "=== Münster Apartment Agent: Intel Mac setup ==="
echo "Mac: $(sw_vers -productVersion) | CPU: $(uname -m)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is missing. Install Python 3.11+ first."
  exit 1
fi

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
[ -f .env ] || cp .env.example .env

echo ""
if command -v llama >/dev/null 2>&1; then
  echo "llama.cpp is already installed: $(command -v llama)"
else
  echo "llama.cpp is not installed."
  echo "Installing with the official llama.cpp installer..."
  curl -LsSf https://llama.app/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo ""
echo "Running apartment rule tests..."
pytest -q

echo ""
echo "Setup finished."
echo "Next open TWO Terminal windows."
echo "Terminal 1: ./start_local_ai.sh"
echo "Terminal 2: source .venv/bin/activate && python -m app.cli doctor"
