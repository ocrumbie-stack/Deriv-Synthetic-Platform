#!/usr/bin/env bash
# Driver for running and visually verifying the Deriv Synthetic Trading
# Platform (FastAPI + server-rendered vanilla-JS dashboard). Run from the
# repo root (the directory this file's .claude/skills/ lives under).
#
# Usage:
#   .claude/skills/run-deriv-synthetic-platform/driver.sh start   # launch on a fresh temp DB
#   .claude/skills/run-deriv-synthetic-platform/driver.sh seed    # fire demo+live webhook signals
#   .claude/skills/run-deriv-synthetic-platform/driver.sh shots   # screenshot the key dashboard pages
#   .claude/skills/run-deriv-synthetic-platform/driver.sh all     # start + seed + shots
#   .claude/skills/run-deriv-synthetic-platform/driver.sh stop    # kill the server, remove temp DB
#
# Env overrides: PORT (default 8125), SHOTS_DIR (default /tmp/run-deriv-shots).
set -euo pipefail

PORT="${PORT:-8125}"
BASE="http://127.0.0.1:$PORT"
# Relative, not absolute: on Windows, sqlite3 chokes on git-bash's
# POSIX-style absolute paths (/c/...) in a sqlite:/// URL ("unable to open
# database file"). A relative path resolves correctly on every platform.
DB_FILE="./run-check.db"
SHOTS_DIR="${SHOTS_DIR:-/tmp/run-deriv-shots}"
PIDFILE="/tmp/run-deriv-uvicorn.pid"
LOGFILE="/tmp/run-deriv-uvicorn.log"

# The venv's own interpreter, invoked by explicit path - do not rely on
# `uvicorn`/`python` already being on PATH or a venv already being active.
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  echo "No .venv found - run: python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt (or .venv/bin/... on Linux/Mac)" >&2
  exit 1
fi

find_browser() {
  local candidates=(
    "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
    "/c/Program Files/Google/Chrome/Application/chrome.exe"
    "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    "/c/Program Files/Microsoft/Edge/Application/msedge.exe"
  )
  for c in "${candidates[@]}"; do
    [ -f "$c" ] && { echo "$c"; return 0; }
  done
  for c in google-chrome google-chrome-stable chromium-browser chromium; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return 0; }
  done
  return 1
}

cmd_start() {
  rm -f "$DB_FILE"
  export DATABASE_URL="sqlite:///$DB_FILE"
  export WEBHOOK_SECRET="${WEBHOOK_SECRET:-test-secret}"
  export EXECUTION_MODE="${EXECUTION_MODE:-demo}"
  # Fake but well-formed credentials so demo/live code paths run (real Deriv
  # calls, real network attempt) instead of short-circuiting on "missing
  # credentials" - they fail at Deriv's API with a 401, which is fine for a
  # UI/wiring smoke check. Override with real ones to check actual execution.
  export DERIV_APP_ID="${DERIV_APP_ID:-1089}"
  export DERIV_DEMO_API_TOKEN="${DERIV_DEMO_API_TOKEN:-fake-demo-token}"
  export DERIV_DEMO_ACCOUNT_ID="${DERIV_DEMO_ACCOUNT_ID:-VRTC0000}"
  export DERIV_API_TOKEN="${DERIV_API_TOKEN:-fake-live-token}"
  export DERIV_ACCOUNT_ID="${DERIV_ACCOUNT_ID:-CR000000}"

  "$PY" -m uvicorn app.main:app --port "$PORT" > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"

  echo "Waiting for $BASE/health ..."
  timeout 30 bash -c "until curl -sf $BASE/health >/dev/null 2>&1; do sleep 1; done" || {
    echo "Server did not come up - see $LOGFILE" >&2
    tail -40 "$LOGFILE" >&2
    exit 1
  }
  curl -s "$BASE/health"; echo
}

cmd_seed() {
  echo "-- demo entry --"
  curl -s -X POST "$BASE/webhook" -H "Content-Type: application/json" -d \
    '{"secret":"'"$WEBHOOK_SECRET"'","strategy":"Run Check","symbol":"R_100","action":"entry","direction":"long","price":1000,"size":1,"signal_id":"run-check-demo-entry"}'
  echo
  sleep 0.5
  echo "-- demo exit --"
  curl -s -X POST "$BASE/webhook" -H "Content-Type: application/json" -d \
    '{"secret":"'"$WEBHOOK_SECRET"'","strategy":"Run Check","symbol":"R_100","action":"exit","price":1050,"signal_id":"run-check-demo-exit"}'
  echo
  sleep 0.5
  echo "-- switch to live --"
  curl -s -X PATCH "$BASE/api/risk" -H "Content-Type: application/json" -d '{"execution_mode":"live"}' | head -c 200; echo
  echo "-- live entry --"
  curl -s -X POST "$BASE/webhook" -H "Content-Type: application/json" -d \
    '{"secret":"'"$WEBHOOK_SECRET"'","strategy":"Run Check","symbol":"R_50","action":"entry","direction":"short","price":500,"size":2,"signal_id":"run-check-live-entry"}'
  echo
  sleep 0.5
  echo "-- switch back to demo --"
  curl -s -X PATCH "$BASE/api/risk" -H "Content-Type: application/json" -d '{"execution_mode":"demo"}' | head -c 200; echo
}

cmd_shots() {
  local chrome
  chrome=$(find_browser) || { echo "No Chrome/Edge found - install one, or screenshot manually." >&2; exit 1; }
  mkdir -p "$SHOTS_DIR"
  # The dashboard is a single page routed by URL hash (see pageIds in
  # app.js) - navigate straight to a page's hash instead of clicking
  # through the sidebar.
  local pages=(dashboard-overview:dashboard trading-history:history signals-journal:signals trading-positions:positions system-risk:risk)
  for p in "${pages[@]}"; do
    local hash="${p%%:*}" name="${p##*:}"
    "$chrome" --headless=new --disable-gpu --no-sandbox --window-size=1500,1000 \
      --virtual-time-budget=8000 --screenshot="$SHOTS_DIR/$name.png" "$BASE/#$hash" >/dev/null 2>&1
    echo "wrote $SHOTS_DIR/$name.png"
  done
}

cmd_stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  rm -f "$DB_FILE"
  echo "stopped, temp db removed"
}

case "${1:-}" in
  start) cmd_start ;;
  seed)  cmd_seed ;;
  shots) cmd_shots ;;
  stop)  cmd_stop ;;
  all)   cmd_start; cmd_seed; cmd_shots ;;
  *) echo "usage: $0 <start|seed|shots|stop|all>" >&2; exit 1 ;;
esac
