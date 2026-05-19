#!/bin/bash
# GERTY Lite — periodic health check
# Scheduled every 5 minutes via Task Scheduler (Windows) or launchd (macOS).
# Checks each component and restarts anything that's down.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GERTY_DIR="${GERTY_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export GERTY_DIR

# Discover Python + lms via the cross-platform module (no hardcoded user paths).
PY_DISCOVER='import sys; sys.path.insert(0, r"'"$GERTY_DIR"'/scripts"); from _paths import find_python, find_lms'
if [[ -z "${GERTY_PYTHON3:-}" ]]; then
  BOOT_PY="$(command -v python3 || command -v python || true)"
  [[ -n "$BOOT_PY" ]] && GERTY_PYTHON3="$("$BOOT_PY" -c "$PY_DISCOVER; print(find_python())" 2>/dev/null || true)"
fi
PYTHON3="${GERTY_PYTHON3:-python3}"
if [[ -n "${GERTY_LMS:-}" && -x "$GERTY_LMS" ]]; then
  LMS="$GERTY_LMS"
elif [[ -x "$PYTHON3" ]]; then
  LMS="$("$PYTHON3" -c "$PY_DISCOVER; r = find_lms(); print(r or '')" 2>/dev/null || true)"
fi
LMS="${LMS:-lms}"

LLM_API="${GERTY_LLM_API:-http://127.0.0.1:1234}"
[[ -f "$GERTY_DIR/config/.model" ]] && source "$GERTY_DIR/config/.model"
MODEL="${GERTY_MODEL:-}"
LOG="$GERTY_DIR/health.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

# Rotate log — keep last 500 lines
if [[ -f "$LOG" ]] && [[ $(wc -l < "$LOG") -gt 600 ]]; then
  tmp=$(tail -500 "$LOG")
  echo "$tmp" > "$LOG"
fi

failures=0

# ── LM Studio API ─────────────────────────────────────────────────────────────
if ! curl -sf "$LLM_API/v1/models" > /dev/null 2>&1; then
  log "FAIL: LM Studio API down — restarting server"
  "$LMS" server start >> "$LOG" 2>&1 || true
  sleep 15
  failures=$((failures + 1))
fi

# ── Configured model ──────────────────────────────────────────────────────────
# IMPORTANT: We do NOT auto-reload the model from automation. Reloading without
# ejecting the existing copy can race with LM Studio's internal state and waste
# VRAM/RAM. If the model is missing here, the user wants to know — not have
# automation silently swap it.
if [[ -n "$MODEL" ]] && ! curl -sf "$LLM_API/v1/models" 2>/dev/null | grep -q "$MODEL"; then
  log "WARN: model $MODEL not in /v1/models — possibly unloaded by LM Studio. NOT auto-reloading; load it manually."
  failures=$((failures + 1))
fi

# ── Kokoro TTS server (optional) ─────────────────────────────────────────────
KOKORO_SCRIPT="${GERTY_KOKORO_TTS:-}"
if [[ -n "$KOKORO_SCRIPT" && -f "$KOKORO_SCRIPT" ]]; then
  if ! curl -sf "http://127.0.0.1:8880/health" > /dev/null 2>&1; then
    log "FAIL: Kokoro TTS server down — restarting"
    nohup "$PYTHON3" "$KOKORO_SCRIPT" >> "$GERTY_DIR/kokoro.log" 2>&1 &
    disown
    failures=$((failures + 1))
  fi
fi

# ── camofox-browser ───────────────────────────────────────────────────────────
if ! curl -sf "http://localhost:9377/health" > /dev/null 2>&1; then
  log "FAIL: camofox down — restarting via PM2"
  pm2 restart camofox >> "$LOG" 2>&1 || pm2 resurrect >> "$LOG" 2>&1 || true
  failures=$((failures + 1))
fi

# ── OmniVoice server (current default TTS) ──────────────────────────────────
if ! curl -sf "http://127.0.0.1:8883/health" > /dev/null 2>&1; then
  log "FAIL: OmniVoice down — restarting via PM2"
  pm2 restart omnivoice >> "$LOG" 2>&1 || pm2 resurrect >> "$LOG" 2>&1 || true
  failures=$((failures + 1))
fi

# ── GERTY Lite bot ────────────────────────────────────────────────────────────
if ! bash "$GERTY_DIR/gerty-lite.sh" status 2>/dev/null | grep -q "Running"; then
  log "FAIL: GERTY Lite down — restarting"
  bash "$GERTY_DIR/gerty-lite.sh" start >> "$LOG" 2>&1
  failures=$((failures + 1))
fi

if [[ $failures -eq 0 ]]; then
  log "OK: all systems nominal"
fi
