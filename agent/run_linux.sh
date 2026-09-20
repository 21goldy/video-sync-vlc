#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python vlc_sync_agent.py
