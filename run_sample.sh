#!/bin/bash
set -e
source .venv/bin/activate
python -m app.cli analyze --file samples/sample_listing.txt
