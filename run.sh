#!/usr/bin/env bash
# Start DVD Cabinet. The first run creates a local virtualenv; GStreamer's Python
# bindings (python3-gi) come from the system, so the venv can see system packages.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up .venv (first run only)..."
  python3 -m venv --system-site-packages .venv
  .venv/bin/pip install --quiet -r requirements.txt
fi
exec .venv/bin/python -m dvdcabinet "$@"
