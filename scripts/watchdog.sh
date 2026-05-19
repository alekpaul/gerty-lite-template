#!/bin/bash
# GERTY Lite watchdog — polls Telegram when session is offline,
# opens a new terminal window on first incoming message.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERTY_DIR="${GERTY_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG="$GERTY_DIR/config/.telegram-config"
PID_FILE="$GERTY_DIR/.gerty-lite.pid"
CTRL_PIPE="$GERTY_DIR/.ctrl-pipe"
OFFSET_FILE="$GERTY_DIR/.watchdog-offset"
WATCHDOG_LOCK="$GERTY_DIR/.watchdog.lock"
RESTART_FLAG="$GERTY_DIR/.restart-flag"

source "$CONFIG"
API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

# Single-instance guard
if [[ -f "$WATCHDOG_LOCK" ]]; then
  existing=$(cat "$WATCHDOG_LOCK")
  if kill -0 "$existing" 2>/dev/null; then
    echo "Watchdog already running (PID $existing)"
    exit 0
  fi
fi
echo $$ > "$WATCHDOG_LOCK"
trap 'rm -f "$WATCHDOG_LOCK"' EXIT INT TERM

session_alive() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid=$(cat "$PID_FILE")
  kill -0 "$pid" 2>/dev/null
}

get_offset() {
  [[ -f "$OFFSET_FILE" ]] && cat "$OFFSET_FILE" || echo "0"
}

save_offset() {
  echo "$1" > "$OFFSET_FILE"
}

send_message() {
  curl -s "$API/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\":\"$TELEGRAM_CHAT_ID\",\"text\":\"$1\"}" > /dev/null 2>&1 || true
}

wake_up() {
  send_message "waking up..."
  bash "$SCRIPT_DIR/../gerty-lite.sh" start
}

send_stop() {
  if [[ -p "$CTRL_PIPE" ]]; then
    echo "stop" > "$CTRL_PIPE" &
    send_message "stopped"
  else
    send_message "no session to stop"
  fi
}

echo "[$(date '+%H:%M:%S')] GERTY Lite watchdog online (PID $$)"

while true; do
  if session_alive; then
    # Session running — let the plugin handle all messages, don't touch getUpdates
    sleep 5
    continue
  fi

  # Session is down — check for restart request first
  if [[ -f "$RESTART_FLAG" ]]; then
    rm -f "$RESTART_FLAG"
    echo "[$(date '+%H:%M:%S')] Restart requested — waking GERTY Lite"
    wake_up
    for i in $(seq 1 20); do sleep 3; session_alive && break; done
    continue
  fi

  # Start polling for incoming messages
  OFFSET=$(get_offset)
  RESPONSE=$(curl -s "$API/getUpdates?offset=${OFFSET}&timeout=30&limit=5" 2>/dev/null) || {
    sleep 5
    continue
  }

  # Parse updates: look for any message (not just commands)
  UPDATES=$(echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for u in data.get('result', []):
        uid = u['update_id']
        msg = u.get('message', {})
        text = msg.get('text', '') or msg.get('caption', '') or '[media]'
        print(f'{uid}|{text[:50]}')
except:
    pass
" 2>/dev/null) || true

  if [[ -z "$UPDATES" ]]; then
    continue
  fi

  # Got at least one message — advance offset past all of them, then wake
  LAST_ID=$(echo "$UPDATES" | tail -1 | cut -d'|' -f1)
  save_offset $((LAST_ID + 1))

  # Drain remaining updates so GERTY Lite starts clean
  curl -s "$API/getUpdates?offset=$((LAST_ID + 1))&limit=1&timeout=0" > /dev/null 2>&1 || true

  echo "[$(date '+%H:%M:%S')] Message received while offline — waking GERTY Lite"
  wake_up

  # Wait for session to come up before resuming watch
  for i in $(seq 1 20); do
    sleep 3
    session_alive && break
  done
done
