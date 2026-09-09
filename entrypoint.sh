#!/bin/sh
set -e

PORT="${PORT:-8000}"

# The Railway volume mounts at /app/data owned by root on first boot;
# fix ownership every start (idempotent, cheap) before dropping to appuser.
mkdir -p /app/data
chown -R appuser:appgroup /app/data 2>/dev/null || true

echo "[entrypoint] Starting Business AI on port $PORT (as appuser)..."
exec gosu appuser uvicorn business_ai.app:create_app --factory --host 0.0.0.0 --port "$PORT"
