#!/usr/bin/env bash
# Honasa mock commerce service (order lookup / returns / tracking / escalations).
cd "$(dirname "$0")/.."
exec env/bin/uvicorn honasa.api.main:app --host 127.0.0.1 --port 9022 "$@"
