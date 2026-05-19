#!/bin/bash
# GERTY Lite — system autostart
# Runs at Windows login via Task Scheduler, or on macOS via launchd.
# Brings up the full stack in the right order:
#   1. LM Studio server (headless, no GUI)
#   2. Wait for API + load the configured model
#   3. Pre-warm voice TTS server (optional)
#   4. Start GERTY Lite bot

set -uo pipefail

# Windows Task Scheduler launches bash with a stripped PATH — without /usr/bin
# we lose nohup, uname, curl, etc., which previously killed this script
# mid-run. Prepending these is a no-op on macOS/Linux (they don't exist) and
# essential on Windows + Git Bash.
export PATH="/usr/bin:/bin:/mingw64/bin:/usr/local/bin:${PATH:-}"

# Auto-discover the repo root (this script lives at scripts/autostart.sh).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERTY_DIR="${GERTY_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export GERTY_DIR

# Resolve LM Studio CLI + Python via the cross-platform discovery module so
# this works on macOS, Linux, and Windows without per-machine edits. Env
# overrides ($GERTY_LMS, $GERTY_PYTHON3) win if set.
PY_DISCOVER='import sys; sys.path.insert(0, r"'"$GERTY_DIR"'/scripts"); from _paths import find_python, find_lms; import sys as s'
if [[ -z "${GERTY_PYTHON3:-}" ]]; then
  # Bootstrap python — accept anything on PATH for the discovery call, then
  # let _paths.py tell us the real one to use for spawned subprocesses.
  BOOT_PY="$(command -v python3 || command -v python || true)"
  if [[ -n "$BOOT_PY" ]]; then
    GERTY_PYTHON3="$("$BOOT_PY" -c "$PY_DISCOVER; print(find_python())" 2>/dev/null || true)"
  fi
fi
PYTHON3="${GERTY_PYTHON3:-python3}"
export GERTY_PYTHON3="$PYTHON3"

# LM Studio CLI — try env, then discovery
if [[ -n "${GERTY_LMS:-}" && -x "$GERTY_LMS" ]]; then
  LMS="$GERTY_LMS"
elif [[ -x "$PYTHON3" ]]; then
  LMS="$("$PYTHON3" -c "$PY_DISCOVER; r = find_lms(); print(r or '')" 2>/dev/null || true)"
fi
LMS="${LMS:-lms}"

# Optional Kokoro TTS warmer (set $GERTY_KOKORO_TTS to the kokoro-server.py path
# to enable). If unset, the warm-up step is skipped — main bot still starts.
TTS_SCRIPT="${GERTY_KOKORO_TTS:-}"
LLM_API="${GERTY_LLM_API:-http://127.0.0.1:1234}"
# Load model selection from single source of truth (config/.model).
# Falls back to defaults if file is missing.
[[ -f "$GERTY_DIR/config/.model" ]] && source "$GERTY_DIR/config/.model"
MODEL="${GERTY_MODEL:-gemma-4-26b-a4b-it-uncensored}"
CONTEXT="${GERTY_CONTEXT:-65536}"
LOG="$GERTY_DIR/autostart.log"
ADMIN_LOG="$GERTY_DIR/admin.log"
ADMIN_PID_FILE="$GERTY_DIR/.admin.pid"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== GERTY autostart ==="

# ── 1. LM Studio server ───────────────────────────────────────────────────────
if [[ -n "$LMS" ]] && command -v "$LMS" >/dev/null 2>&1 || [[ -x "$LMS" ]]; then
  log "Starting LM Studio server via $LMS..."
  "$LMS" server start >> "$LOG" 2>&1 || true
else
  log "WARNING: lms CLI not found — LM Studio must be running manually or install lms via the LM Studio app"
fi

# Wait up to 60s for the API to respond
log "Waiting for LM Studio API at $LLM_API..."
for i in $(seq 1 30); do
  if curl -sf "$LLM_API/v1/models" > /dev/null 2>&1; then
    log "LM Studio API ready (after ${i}x2s)"
    break
  fi
  sleep 2
done

# Verify it's up
if ! curl -sf "$LLM_API/v1/models" > /dev/null 2>&1; then
  log "WARNING: LM Studio API not responding after 60s — continuing anyway"
fi

# ── 2. Load the configured model ─────────────────────────────────────────────
if [[ -n "$LMS" ]] && (command -v "$LMS" >/dev/null 2>&1 || [[ -x "$LMS" ]]); then
  log "Loading model $MODEL..."
  "$LMS" load "$MODEL" -c "$CONTEXT" >> "$LOG" 2>&1 || true
  log "Model load issued."
fi

# Wait for model to appear in /v1/models (up to 120s)
for i in $(seq 1 60); do
  if curl -sf "$LLM_API/v1/models" 2>/dev/null | grep -q "$MODEL"; then
    log "Model loaded (after ${i}x2s)"
    break
  fi
  sleep 2
done

# ── 3. Pre-warm Kokoro TTS server (optional) ─────────────────────────────────
if [[ -n "$TTS_SCRIPT" && -f "$TTS_SCRIPT" ]]; then
  log "Pre-warming Kokoro TTS server via $TTS_SCRIPT..."
  WARM_OUT="${TMPDIR:-/tmp}/gerty-warmup.ogg"
  "$PYTHON3" "$TTS_SCRIPT" "ready" --output "$WARM_OUT" >> "$LOG" 2>&1 &
  log "Kokoro warm-up launched in background."
else
  log "Kokoro TTS warm-up skipped (set GERTY_KOKORO_TTS to enable)."
fi

# ── 4. Start GERTY Lite bot ───────────────────────────────────────────────────
log "Starting GERTY Lite..."
bash "$GERTY_DIR/gerty-lite.sh" start

# ── 5. Start admin dashboard (http://127.0.0.1:9090) ─────────────────────────
log "Starting admin dashboard..."
ADMIN_ALIVE=0
if [[ -f "$ADMIN_PID_FILE" ]]; then
  ADMIN_PID="$(cat "$ADMIN_PID_FILE" 2>/dev/null || true)"
  if [[ -n "$ADMIN_PID" ]] && "$PYTHON3" -c "import sys; sys.path.insert(0, r'$GERTY_DIR/scripts'); from _proc import pid_alive; sys.exit(0 if pid_alive($ADMIN_PID) else 1)" 2>/dev/null; then
    ADMIN_ALIVE=1
    log "admin already running (pid $ADMIN_PID)"
  fi
fi
if [[ $ADMIN_ALIVE -eq 0 ]]; then
  nohup "$PYTHON3" "$GERTY_DIR/admin/server.py" >> "$ADMIN_LOG" 2>&1 &
  echo $! > "$ADMIN_PID_FILE"
  disown
  log "admin launched (pid $(cat "$ADMIN_PID_FILE"))"
fi

# ── 6. Start watchdog (wake-on-message when bot is offline) ──────────────────
log "Starting watchdog..."
nohup bash "$GERTY_DIR/scripts/watchdog.sh" >> "$GERTY_DIR/watchdog.log" 2>&1 &
disown

# ── 7. Start gerty-live (realtime voice WS) + cloudflared tunnel ─────────────
# Spawns the WS voice server, opens a trycloudflare quick tunnel, and DMs the
# magic-link URL via Telegram. Token rotates per restart.
# Runs BEFORE the pm2 step because pm2's npm shim has historically crashed
# Task-Scheduler bash hard (STATUS_DLL_INIT_FAILED) — keeping gerty-live ahead
# of it guarantees the web UI comes up even when pm2 explodes.
# gerty-live is an OPTIONAL sibling repo. If $GERTY_LIVE_DIR is set OR the
# sibling exists at ../gerty-live, we boot it; otherwise we silently skip.
GERTY_LIVE_DIR_RESOLVED="${GERTY_LIVE_DIR:-$(cd "$GERTY_DIR/../gerty-live" 2>/dev/null && pwd || true)}"
LIVE_LAUNCH=""
if [[ -n "$GERTY_LIVE_DIR_RESOLVED" ]]; then
  LIVE_LAUNCH="$GERTY_LIVE_DIR_RESOLVED/scripts/live-up.sh"
fi
if [[ -n "$LIVE_LAUNCH" ]] && [[ -x "$LIVE_LAUNCH" || -f "$LIVE_LAUNCH" ]]; then
  log "Starting gerty-live + tunnel from $GERTY_LIVE_DIR_RESOLVED..."
  bash "$LIVE_LAUNCH" >> "$LOG" 2>&1 || log "WARNING: live-up.sh failed — see $LOG"
else
  log "gerty-live not configured — skipping (set GERTY_LIVE_DIR to enable)"
fi

# ── 8. Resurrect PM2 (camofox-browser) ───────────────────────────────────────
# Isolated in a `&`-backgrounded subshell with `disown` so that if the npm shim
# blows up with STATUS_DLL_INIT_FAILED (the WSL relay crash we saw in autostart
# logs), it can't take the parent bash down with it. Camofox is optional — only
# the `browser_open` tool needs it — so failure here is fine.
log "Resurrecting PM2 (camofox-browser) in background..."
(
  pm2 resurrect >> "$LOG" 2>&1 \
    && log "pm2 resurrect completed" \
    || log "WARNING: pm2 resurrect failed — camofox may need manual start"
  # Verify camofox responds within 30s
  for i in $(seq 1 15); do
    if curl -sf "http://localhost:9377/health" > /dev/null 2>&1; then
      log "camofox ready (after ${i}x2s)"
      break
    fi
    sleep 2
  done
) >> "$LOG" 2>&1 &
disown

log "=== Autostart complete ==="
