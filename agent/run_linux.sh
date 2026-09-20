#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "Creating Linux virtual environment..."
  python3 -m venv .venv || {
    echo "Python venv is missing. Install with: sudo apt install python3-full python3-venv"
    exit 1
  }
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python vlc_sync_agent.py
