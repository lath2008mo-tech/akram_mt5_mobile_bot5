#!/usr/bin/env bash
# Self-healing launcher for the mobile preview / API on Linux or macOS.
# Recreates the virtualenv if it is missing, then serves on port 8000.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[serve] creating virtualenv"
  python3 -m venv .venv
fi
if ! .venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[serve] installing dependencies"
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

exec .venv/bin/python -m uvicorn main:app \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --proxy-headers --forwarded-allow-ips="*"
