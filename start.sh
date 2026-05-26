#!/bin/sh
set -eu
cd "$(dirname "$0")"

echo "Starting OutboundAI..."
python -c "from db import validate_runtime_config; import sys; sys.exit(1 if validate_runtime_config() else 0)"

echo "Starting FastAPI server on port 8000..."
uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

sleep 2
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "FastAPI server failed to start"
  exit 1
fi

echo "Starting LiveKit agent worker..."
python agent.py start &
AGENT_PID=$!

while kill -0 "$SERVER_PID" 2>/dev/null && kill -0 "$AGENT_PID" 2>/dev/null; do
  sleep 2
done

EXIT_CODE=0
if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  wait "$SERVER_PID" || EXIT_CODE=$?
fi
if ! kill -0 "$AGENT_PID" 2>/dev/null; then
  wait "$AGENT_PID" || EXIT_CODE=$?
fi

kill "$SERVER_PID" "$AGENT_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
wait "$AGENT_PID" 2>/dev/null || true
exit "$EXIT_CODE"
