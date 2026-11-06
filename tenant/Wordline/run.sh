#!/usr/bin/env bash
# Wordline merchant data API — standalone tenant service (CSV → JSON → HTTP).
# Binds to loopback only; the platform's api_connections rows point at it.
# Requires WORDLINE_MERCHANT_API_KEY (read from the repo .env when unset).
cd "$(dirname "$0")/../.."
exec env/bin/uvicorn tenant.Wordline.api.main:app --host 127.0.0.1 \
  --port "${WORDLINE_MERCHANT_API_PORT:-9023}" "$@"
