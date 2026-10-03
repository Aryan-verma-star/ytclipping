#!/usr/bin/env bash
# Live verification of the deployment-prep changes:
#   1. backend boots with CLIPPER_ALLOWED_ORIGINS set (CORS on)
#   2. preflight OPTIONS from the allowed origin -> 200 + CORS headers
#   3. disallowed origin gets no CORS headers on plain GET
#   4. cookies-from-env materializes a 0600 file when CLIPPER_YTDLP_COOKIES set
#   5. boot WITHOUT the origin var -> no CORS headers (same-origin mode)
# Uses port 8010 to avoid clashing with the sandbox's :8000.
set -uo pipefail

cd "$(dirname "$0")/../backend"
PORT=8010
LOG=$(mktemp)
FAILS=0

check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS: $1"; else echo "FAIL: $1 (expected [$2] got [$3])"; FAILS=$((FAILS+1)); fi
}

start_env() { # extra env vars...
  env CLIPPER_ENVIRONMENT=production CLIPPER_DATA_DIR=/tmp/cors-verify-data "$@" \
    python3 -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
  UVICORN_PID=$!
  for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  echo "backend failed to start:"; tail -5 "$LOG"; exit 1
}

stop_backend() { kill "$UVICORN_PID" 2>/dev/null; wait "$UVICORN_PID" 2>/dev/null; }

echo "== 1-3: CORS-enabled boot =="
rm -rf /tmp/cors-verify-data
start_env CLIPPER_ALLOWED_ORIGINS=https://youtube-clipper.vercel.app

ACAO=$(curl -s -o /dev/null -D - -X OPTIONS "http://127.0.0.1:$PORT/api/jobs" \
  -H "Origin: https://youtube-clipper.vercel.app" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" \
  | tr -d '\r' | grep -i '^access-control-allow-origin:' | awk '{print $2}')
check "preflight from allowed origin returns ACAO" "https://youtube-clipper.vercel.app" "$ACAO"

STATUS=$(curl -s -o /dev/null -w '%{http_code}' -X OPTIONS "http://127.0.0.1:$PORT/api/jobs" \
  -H "Origin: https://youtube-clipper.vercel.app" -H "Access-Control-Request-Method: POST")
check "preflight status" "200" "$STATUS"

ACAO_EVIL=$(curl -s -o /dev/null -D - "http://127.0.0.1:$PORT/api/meta" \
  -H "Origin: https://evil.example" | tr -d '\r' \
  | grep -ci '^access-control-allow-origin:' || true)
check "disallowed origin gets no ACAO" "0" "$ACAO_EVIL"

HEALTH=$(curl -s "http://127.0.0.1:$PORT/api/health" | grep -o '"status":"[a-z]*"')
check "health OK with CORS on" '"status":"ok"' "$HEALTH"
stop_backend

echo "== 4: cookies from env =="
rm -rf /tmp/cors-verify-data
start_env CLIPPER_YTDLP_COOKIES="# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	0	CONSENT	YES+cb"
PERMS=$(stat -c '%a' /tmp/cors-verify-data/cookies-from-env.txt 2>/dev/null)
check "cookies file materialized with 0600 perms" "600" "$PERMS"
stop_backend

echo "== 5: default boot (no CORS vars) =="
rm -rf /tmp/cors-verify-data
start_env
ACAO_NONE=$(curl -s -o /dev/null -D - "http://127.0.0.1:$PORT/api/meta" \
  -H "Origin: https://youtube-clipper.vercel.app" | tr -d '\r' \
  | grep -ci '^access-control-allow-origin:' || true)
check "default same-origin boot has no CORS headers" "0" "$ACAO_NONE"
stop_backend

rm -f "$LOG"
if [ "$FAILS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$FAILS CHECK(S) FAILED"; exit 1; fi
