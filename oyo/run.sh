#!/usr/bin/env bash
# Start the OYO mock integration service (port 9021).
# Usage: ./oyo/run.sh   (from the repo root)
cd "$(dirname "$0")/.." || exit 1
exec env/bin/uvicorn oyo.api.main:app --host 127.0.0.1 --port 9021 "$@"
