#!/usr/bin/env bash
# Start the CCTV server with production-grade settings.
#
# Usage: scripts/run.sh [PORT]
#
# Environment variables (all optional):
#   PORT      – listen port           (default 8000)
#   HOST      – bind address          (default 0.0.0.0)
#   WORKERS   – gunicorn workers      (default: 2×CPU cores, min 4)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-${PORT:-8000}}"
HOST="${HOST:-0.0.0.0}"

# Default workers: 2×CPU cores, minimum 4 for 200+ concurrent users.
if [ -z "${WORKERS:-}" ]; then
  CPUS=$(python3 -c "import os; print(os.cpu_count() or 2)")
  WORKERS=$(( CPUS * 2 ))
  [ "$WORKERS" -lt 4 ] && WORKERS=4
fi

echo "Starting CCTV server: ${WORKERS} workers on ${HOST}:${PORT}"

# Prefer the project virtualenv so the script works without activating it first.
GUNICORN="gunicorn"
if [ -x ".venv/bin/gunicorn" ]; then
  GUNICORN=".venv/bin/gunicorn"
fi

exec "${GUNICORN}" app.main:app \
  -c gunicorn.conf.py \
  -k uvicorn.workers.UvicornWorker \
  --workers "${WORKERS}" \
  --bind "${HOST}:${PORT}" \
  --timeout 300 \
  --graceful-timeout 30 \
  --keep-alive 5 \
  --max-requests 10000 \
  --max-requests-jitter 500 \
  --forwarded-allow-ips '*' \
  --access-logfile - \
  --error-logfile -
