#!/usr/bin/env bash
# Start the code review service. Port defaults to 8083 (override with PORT=...).
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv && uv pip install -p .venv/bin/python -r requirements-dev.txt
  else
    python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
  fi
fi
PORT="${PORT:-8083}"
HOST="${HOST:-0.0.0.0}"
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
