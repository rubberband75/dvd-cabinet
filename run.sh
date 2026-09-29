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
# Every playback session starts dozens of short-lived GStreamer threads; by default glibc
# gives each its own memory arena and never returns them, so memory creeps up per session.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
exec .venv/bin/python -m dvdcabinet "$@"
