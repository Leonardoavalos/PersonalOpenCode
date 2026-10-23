#!/usr/bin/env bash
# Start the local OpenCode server and the Telegram bridge together.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "missing .env; run: cp env.example .env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env
set +a

: "${OPENCODE_PASSWORD:?set OPENCODE_PASSWORD in .env}"
PORT="${OPENCODE_PORT:-4096}"
export OPENCODE_URL="http://127.0.0.1:${PORT}"

# Keep the owner's global OpenCode config out of every turn. Without this the
# global instruction files add ~10k cached input tokens and up to 5x the
# reasoning tokens per reply. Override by setting OPENCODE_CONFIG_DIR in .env.
export OPENCODE_CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$PWD/bot-config}"

opencode serve --hostname 127.0.0.1 --port "$PORT" > serve.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  if curl -sf -u "opencode:${OPENCODE_PASSWORD}" "${OPENCODE_URL}/api/info" > /dev/null; then
    break
  fi
  sleep 1
done

python3 bot.py
