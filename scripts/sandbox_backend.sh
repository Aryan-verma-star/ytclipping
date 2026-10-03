#!/usr/bin/env bash
# Start the FastAPI backend inside the build sandbox on port 8000.
# The Caddy gateway exposes it externally via any URL that includes
# ?XTransformPort=8000 — the Next.js "/" page embeds it full-screen.
set -euo pipefail

cd "$(dirname "$0")/../backend"

exec python3 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info
