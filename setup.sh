#!/usr/bin/env bash
# Run once per install by viam-server (`first_run` in meta.json).
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    python3 -m venv venv
fi

./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
