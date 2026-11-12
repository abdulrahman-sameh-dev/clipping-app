#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
CLI_BIN="$ROOT_DIR/.venv/bin/clipping-ai"
LOG_FILE="$(mktemp -t clipping-studio-start.XXXXXX.log)"
SERVER_PID=""
TAIL_PID=""

info() {
  printf '%s\n' "$*"
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local status="${1:-$?}"
  trap - EXIT INT TERM
  if [[ -n "$TAIL_PID" ]] && kill -0 "$TAIL_PID" 2>/dev/null; then
    kill "$TAIL_PID" 2>/dev/null || true
  fi
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    info "Stopping Clipping Studio..."
    kill -INT "$SERVER_PID" 2>/dev/null || true
    for _ in {1..40}; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      kill -TERM "$SERVER_PID" 2>/dev/null || true
    fi
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  exit "$status"
}

trap cleanup EXIT
trap 'cleanup 130' INT TERM

command -v curl >/dev/null 2>&1 || fail "curl is required to verify local startup."
command -v tail >/dev/null 2>&1 || fail "tail is required to stream server logs."
[[ -x "$PYTHON_BIN" ]] || fail "Python environment missing. Run: uv sync --extra dev"
[[ -x "$CLI_BIN" ]] || fail "clipping-ai command missing. Run: uv sync --extra dev"

"$PYTHON_BIN" -c 'import fastapi, uvicorn' >/dev/null 2>&1 \
  || fail "Python dependencies are incomplete. Run: uv sync --extra dev"

read -r HOST PORT < <("$PYTHON_BIN" -c \
  'from clipping_ai.settings import get_settings; s = get_settings(); print(s.host, s.port)')

[[ -n "$HOST" && -n "$PORT" ]] || fail "Could not resolve CLIPPING_AI_HOST and CLIPPING_AI_PORT."

URL_HOST="$HOST"
if [[ "$HOST" == "0.0.0.0" ]]; then
  URL_HOST="127.0.0.1"
elif [[ "$HOST" == *:* && "$HOST" != \[*\] ]]; then
  URL_HOST="[$HOST]"
fi

BASE_URL="http://${URL_HOST}:${PORT}"
HEALTH_URL="${BASE_URL}/health"
UI_URL="${BASE_URL}/ui/"

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
  info "Warning: CLIPPING_AI_HOST=$HOST is not loopback. This API has no authentication."
fi

if [[ -f .env ]]; then
  ENV_MODE="$(stat -c '%a' .env 2>/dev/null || true)"
  if [[ "$ENV_MODE" =~ ^[0-7]+$ ]] && (( (8#$ENV_MODE & 077) != 0 )); then
    info "Warning: .env is readable by group/others. Run: chmod 600 .env"
  fi
else
  info "No .env found. The service will start, but providers without keys stay disabled."
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  info "Warning: ffmpeg/ffprobe not found. Video preparation and clip rendering will fail."
fi

if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "$UI_URL" >/dev/null 2>&1; then
    info "Clipping Studio is already running at $UI_URL"
    info "Not starting another server. Press Ctrl+C in the existing terminal to stop it."
    exit 0
  fi
  fail "A backend is already responding at $HEALTH_URL, but $UI_URL is unavailable. Stop that process first."
fi

if command -v ss >/dev/null 2>&1 && [[ -n "$(ss -H -ltn "sport = :$PORT" 2>/dev/null)" ]]; then
  fail "Port $PORT is already in use by another process. Set CLIPPING_AI_PORT to a free port or stop that process."
fi

info "Starting Clipping Studio backend on $BASE_URL"
info "The packaged frontend is served by this backend; no separate frontend server is needed."
info "Logs: $LOG_FILE"
info "Press Ctrl+C to stop."

: >"$LOG_FILE"
"$CLI_BIN" serve --host "$HOST" --port "$PORT" >"$LOG_FILE" 2>&1 &
SERVER_PID=$!
tail -n +1 -F "$LOG_FILE" &
TAIL_PID=$!

READY=0
EXIT_CODE=0
for _ in {1..60}; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID" 2>/dev/null || EXIT_CODE=$?
    info "Backend failed to start (exit ${EXIT_CODE}). Recent log output:"
    tail -n 40 "$LOG_FILE" || true
    exit 1
  fi
  if curl -fsS --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.5
done

if [[ "$READY" -ne 1 ]]; then
  info "Backend did not become healthy at $HEALTH_URL within 30 seconds. Recent log output:"
  tail -n 40 "$LOG_FILE" || true
  exit 1
fi

for path in "/api/ai/status?include_health=false" "/api/video/jobs?limit=1" "/api/clip-jobs?limit=1"; do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${BASE_URL}${path}" || true)"
  if [[ "$CODE" != "200" ]]; then
    info "Backend API check failed: ${BASE_URL}${path} returned ${CODE:-no response}."
    exit 1
  fi
done

for path in "/ui/" "/ui/styles.css" "/ui/js/app.js"; do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "${BASE_URL}${path}" || true)"
  if [[ "$CODE" != "200" ]]; then
    info "Frontend check failed: ${BASE_URL}${path} returned ${CODE:-no response}."
    exit 1
  fi
done

UI_HTML="$(curl -fsS --max-time 5 "$UI_URL" 2>/dev/null || true)"
if [[ "$UI_HTML" != *'class="app-shell"'* ]]; then
  info "Frontend check failed: $UI_URL responded but did not contain the studio shell."
  exit 1
fi

info ""
info "Clipping Studio is ready."
info "Open: $UI_URL"
info "Health: $HEALTH_URL"
info "Logs: $LOG_FILE"
info "Press Ctrl+C to stop the backend and frontend."

wait "$SERVER_PID"
